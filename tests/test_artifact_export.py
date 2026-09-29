from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import errno
from hashlib import sha256
import json
from time import monotonic

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    PostgresToolArtifactReadPort,
    ToolOutputArtifactProcessor,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
    ToolDispatchAuthorizationRequest,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.artifact import (
    ToolArtifactBodyView,
    ToolArtifactRecordView,
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.primitives.run_permission import (
    PermissionMode,
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.primitives.tool_result_projection import (
    classify_tool_result_delivery,
    ToolResultFullDeliveryReason,
)
from pulsara_agent.tools.builtins.artifact import ArtifactExportTool
from pulsara_agent.tools.builtins import filesystem
from pulsara_agent.tools.builtins.workspace import WritePathScope
from tests.support.postgres import verified_postgres_provider
from tests.test_round1_tool_output_artifact import (
    _install_tool_call,
    _name,
    _processor,
    _RecordingPublisher,
)


class BodyPort:
    def __init__(self, content=b"retained text", *, snapshot=False):
        self.reads = 0
        self.record = ToolArtifactRecordView(
            artifact_id="artifact:test",
            role="OUTPUT",
            media_type="text/plain",
            size_bytes=len(content),
            artifact_disposition=ToolOutputArtifactDisposition.INCOMPLETE
            if snapshot
            else ToolOutputArtifactDisposition.AVAILABLE,
            source_coverage=ToolOutputSourceCoverage.RETAINED_SNAPSHOT
            if snapshot
            else ToolOutputSourceCoverage.COMPLETE,
            display_kind=ToolResultDisplayKind.HEAD_TAIL,
            source_coverage_reason=ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP
            if snapshot
            else None,
            artifact_unavailability_reason=None,
            blob_id="blob:test",
            digest="sha256:" + sha256(content).hexdigest(),
            codec="utf-8",
            accepted_at_utc="2026-09-30T00:00:00Z",
            model_visible_memory_fact_ids=("memory:not-displayed",),
        )
        self.content = content

    def read_body(self, artifact_id):
        self.reads += 1
        if artifact_id != self.record.artifact_id:
            raise KeyError(artifact_id)
        return ToolArtifactBodyView(self.record, self.content)


def call(path="output.txt", artifact_id="artifact:test", **extra):
    return ToolCall(
        id="call:export",
        name="artifact_export",
        arguments={
            "artifact_id": artifact_id,
            "path": str(path),
            **extra,
        },
    )


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xef\xbb\xbfA\r\n\x00end",
        '{"n":1e99,"n":2,"s":"中文🙂"}'.encode(),
        b"a" * 100000,
    ],
)
@pytest.mark.parametrize("snapshot", [False, True])
def test_export_exact_bytes_coverage_and_nonrecursive_location(
    tmp_path, content, snapshot
):
    port = BodyPort(content, snapshot=snapshot)
    result = ArtifactExportTool(tmp_path, artifact_read_port=port).execute(
        call("nested/result.bin")
    )
    assert result.status is ToolResultState.SUCCESS
    payload = json.loads(result.output)
    target = tmp_path / "nested/result.bin"
    assert target.read_bytes() == content
    assert payload == {
        "status": "success",
        "artifact_id": "artifact:test",
        "path": str(target),
        "bytes_written": len(content),
        "source_coverage": port.record.source_coverage.value,
        "source_coverage_reason": port.record.source_coverage_reason.value
        if snapshot
        else None,
    }
    assert target.stat().st_mode & 0o777 == 0o600
    assert port.reads == 1
    assert result.model_visible_memory_fact_ids == ()
    assert result.artifact_inline_result
    assert result.output_artifact_candidate is None
    publisher = _RecordingPublisher()
    projected = _processor(publisher).prepare(
        workspace_id="workspace:test",
        result_entry_id="entry:test",
        public_output=result.output,
        candidate=None,
        artifact_inline_result=result.artifact_inline_result,
        deadline_monotonic=monotonic() + 30,
    )
    assert not publisher.calls
    assert projected.artifact_id is None
    assert projected.canonical_preview.canonical_bytes.decode() == result.output
    assert (
        classify_tool_result_delivery(
            tool_name=result.tool_name, result_state="SUCCESS"
        ).reason
        is ToolResultFullDeliveryReason.ARTIFACT_EXPORT_LOCATION
    )


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
    result = ArtifactExportTool(tmp_path, artifact_read_port=BodyPort()).execute(call())
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
        return ArtifactExportTool(
            tmp_path, artifact_read_port=BodyPort(content)
        ).execute(call())

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
    result = ArtifactExportTool(tmp_path, artifact_read_port=BodyPort()).execute(call())
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
    result = ArtifactExportTool(tmp_path, artifact_read_port=BodyPort()).execute(call())
    assert json.loads(result.output)["error"] == "FILE_PUBLICATION_UNCONFIRMED"
    assert "No destination was created" not in result.output
    assert (tmp_path / "output.txt").read_bytes() == b"retained text"
    assert len(closed_after_failure) == 2
    assert not list(tmp_path.glob(".pulsara-*.tmp"))


@pytest.mark.parametrize("child", [False, True])
def test_export_cancel_after_publication_keeps_exact_single_outcome(
    tmp_path, monkeypatch, child
):
    from threading import Event
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.tools.builtins import artifact
    from tests.support.round3 import invoke_direct_tool

    published = Event()
    release = Event()
    writes = []
    actual_create = artifact._atomic_create_bytes

    def create(path, content):
        actual_create(path, content)
        writes.append(path)
        published.set()
        release.wait()

    monkeypatch.setattr(artifact, "_atomic_create_bytes", create)

    async def run():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:export",
            session_id="session:export",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            artifact_read_port=BodyPort(),
        )
        operation = asyncio.create_task(
            invoke_direct_tool(
                port,
                session_id="session:export",
                tool_name="artifact_export",
                arguments={"artifact_id": "artifact:test", "path": "output.txt"},
                tool_call_id="call:export",
                attempt_id="attempt:export",
                turn_id="turn:export",
                assistant_entry_id="entry:export",
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK
                if child
                else ModelInputScopeKind.ROOT,
                scope_subagent_task_id="task:export" if child else None,
            )
        )
        try:
            assert await asyncio.to_thread(published.wait, 5)
            operation.cancel()
            await asyncio.sleep(0)
            release.set()
            result = await operation
            assert result.state == "SUCCESS"
            assert result.caller_cancelled_while_running
            assert result.artifact_inline_result
            assert json.loads(result.content)["status"] == "success"
            assert writes == [tmp_path / "output.txt"]
            assert (tmp_path / "output.txt").read_bytes() == b"retained text"
        finally:
            release.set()
            await port.aclose()

    asyncio.run(run())


def test_export_available_to_root_and_child_with_same_scoped_owner(tmp_path):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from tests.support.round3 import prepare_test_direct_tool_surface

    async def run():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:export",
            session_id="session:export",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            artifact_read_port=BodyPort(),
        )
        try:
            for scope, task in [
                (ModelInputScopeKind.ROOT, None),
                (ModelInputScopeKind.SUBAGENT_TASK, "task:export"),
            ]:
                surface = prepare_test_direct_tool_surface(
                    port, conversation_scope_kind=scope, scope_subagent_task_id=task
                )
                assert {"artifact_read", "artifact_export"}.issubset(
                    {s.name for s in surface.model_surface.tool_specs}
                )
        finally:
            await port.aclose()

    asyncio.run(run())


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
    result = ArtifactExportTool(tmp_path, artifact_read_port=BodyPort()).execute(call())
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
    result = ArtifactExportTool(tmp_path, artifact_read_port=BodyPort()).execute(
        call("parent/output.txt")
    )
    assert result.status is ToolResultState.ERROR
    assert not (other / "output.txt").exists()
    assert (relocated / "output.txt").exists() is after_publish
    assert json.loads(result.output)["error"] == (
        "FILE_PUBLICATION_UNCONFIRMED" if after_publish else "FILE_CREATE_FAILED"
    )
    assert not list(relocated.glob(".pulsara-*.tmp"))


def test_large_serialized_metadata_fails_before_writing(tmp_path):
    port = BodyPort()
    artifact_id = '"' * 40000
    port.record = replace(port.record, artifact_id=artifact_id)
    result = ArtifactExportTool(tmp_path, artifact_read_port=port).execute(
        call(artifact_id=artifact_id)
    )
    assert json.loads(result.output)["status"] == "resource_boundary"
    assert not list(tmp_path.iterdir())
    # Unknown overlong IDs also produce a bounded failure without echoing them.
    result = ArtifactExportTool(tmp_path, artifact_read_port=BodyPort()).execute(
        call(artifact_id=artifact_id)
    )
    assert len(result.output) < 1000


def test_inline_metadata_above_archive_threshold_does_not_create_an_artifact(tmp_path):
    port = BodyPort()
    port.record = replace(port.record, artifact_id="a" * 9000)
    result = ArtifactExportTool(tmp_path, artifact_read_port=port).execute(
        call(artifact_id=port.record.artifact_id)
    )
    assert result.status is ToolResultState.SUCCESS
    assert len(result.output.encode()) > 8000
    publisher = _RecordingPublisher()
    projected = _processor(publisher).prepare(
        workspace_id="workspace:test",
        result_entry_id="entry:large-export",
        public_output=result.output,
        candidate=None,
        artifact_inline_result=True,
        deadline_monotonic=monotonic() + 30,
    )
    assert projected.artifact_id is None
    assert not publisher.calls
    assert projected.canonical_preview.canonical_bytes.decode() == result.output


@pytest.mark.parametrize(
    "extra", [{"overwrite": True}, {"encoding": "ascii"}, {"mode": "json"}]
)
def test_unknown_parameters_fail_without_read_or_write(tmp_path, extra):
    port = BodyPort()
    result = ArtifactExportTool(tmp_path, artifact_read_port=port).execute(
        call(**extra)
    )
    assert result.status is ToolResultState.ERROR
    assert port.reads == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("outside", [False, True])
def test_export_uses_existing_file_write_permissions(tmp_path, mode, outside):
    path = tmp_path.parent / "outside.txt" if outside else tmp_path / "result.txt"
    permission = build_run_permission_snapshot(
        snapshot_id="permission:export",
        requested_mode=mode,
        effective_mode=mode,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    result = asyncio.run(
        DefaultToolDispatchAuthorizationPolicy().decide(
            ToolDispatchAuthorizationRequest(
                "artifact_export",
                "call:export",
                {"artifact_id": "artifact:test", "path": str(path)},
                "turn:export",
                "entry:export",
                permission,
                tmp_path,
            )
        )
    )
    expected = (
        "DENY"
        if mode is PermissionMode.READ_ONLY
        else (
            "ALLOW"
            if mode is PermissionMode.BYPASS_PERMISSIONS
            or (mode is PermissionMode.ACCEPT_EDITS and not outside)
            else "REQUIRE_CONFIRMATION"
        )
    )
    assert result.kind.value == expected
    assert (
        builtin_tool_catalog_entry("artifact_export").recovery_contract.severity
        == "bounded_write"
    )


def test_export_relative_and_authorized_home_paths(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    tool = ArtifactExportTool(workspace, artifact_read_port=BodyPort())
    denied = tool.execute(call("~/out.txt"))
    assert denied.status is ToolResultState.ERROR
    allowed = tool.execute(call("~/out.txt"), write_scope=WritePathScope.HOST_LOCAL)
    assert json.loads(allowed.output)["path"] == str(home / "out.txt")
    assert (home / "out.txt").read_bytes() == b"retained text"


def test_postgres_export_scope_corruption_and_deleted_edge(
    stage2_migrated_postgres_database, tmp_path
):
    database = stage2_migrated_postgres_database
    provider = verified_postgres_provider(database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_entry_id, tool_call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    source = ('{"duplicate":1,"duplicate":2,"text":"中🙂"}\r\n' * 600).encode()
    result_entry_id = _name("entry")
    projection = ToolOutputArtifactProcessor(provider).prepare(
        workspace_id=workspace_id,
        result_entry_id=result_entry_id,
        public_output=source.decode(),
        candidate=None,
        artifact_inline_result=False,
        deadline_monotonic=monotonic() + 30,
    )
    accepted = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="terminal",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
    )
    repository.accept_tool_result(
        lease.guard, candidate=accepted, deadline_monotonic=monotonic() + 30
    )

    def exporter(session_id=lease.guard.session_id, workspace=workspace_id):
        return ArtifactExportTool(
            tmp_path,
            artifact_read_port=PostgresToolArtifactReadPort(
                provider,
                session_id=session_id,
                workspace_id=workspace,
            ),
        )

    artifact_id = projection.artifact_id
    for tool in (
        exporter(session_id="session:unknown"),
        exporter(workspace="workspace:unknown"),
    ):
        assert (
            json.loads(tool.execute(call(artifact_id=artifact_id)).output)["status"]
            == "not_found"
        )
        assert not (tmp_path / "output.txt").exists()
    assert (
        exporter().execute(call(artifact_id=artifact_id)).status
        is ToolResultState.SUCCESS
    )
    assert (tmp_path / "output.txt").read_bytes() == source
    import psycopg

    with psycopg.connect(database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
            (b"x" * len(source), projection.artifact_blob.blob_id),
        )
    failed = exporter().execute(call("never.txt", artifact_id=artifact_id))
    assert json.loads(failed.output)["status"] == "content_error"
    assert not (tmp_path / "never.txt").exists()
    # Simulate a missing canonical reference in this disposable database. A
    # remaining global blob must not become an alternate access path.
    with psycopg.connect(database.admin_dsn) as connection:
        connection.execute("SET LOCAL session_replication_role = replica")
        connection.execute(
            "DELETE FROM pulsara_v3.tool_results WHERE session_id=%s AND output_artifact_id=%s",
            (lease.guard.session_id, artifact_id),
        )
    missing = exporter().execute(call("deleted.txt", artifact_id=artifact_id))
    assert json.loads(missing.output)["status"] == "not_found"
    assert not (tmp_path / "deleted.txt").exists()


@pytest.mark.parametrize("terminal_available", [True, False])
def test_export_runtime_dispatch_delivery_and_prefix_continuity(
    stage2_migrated_postgres_database, tmp_path, terminal_available
):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.llm.input import LLMTextPart, MessageRole
    from tests.support.model_config import (
        frozen_test_prompt,
        test_model_resolution_snapshot,
    )
    from tests.support.round3 import (
        ScriptedKernelModel,
        StaticContextSourceCollector,
        seal_test_direct_tool_port,
    )
    from tests.test_round5_long_horizon_postgres import (
        _lease,
        _tool_stream,
        _text_stream,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _, lease = _lease(repository)
    model = ScriptedKernelModel(
        [
            _tool_stream(
                1,
                tool_name="artifact_export",
                arguments='{"artifact_id":"artifact:test","path":"result.txt"}',
            ),
            _tool_stream(2, tool_name="read_file", arguments='{"path":"result.txt"}'),
            _text_stream("source checked", block_id="answer"),
        ]
    )

    async def run():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:export",
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            artifact_read_port=BodyPort(b"exact source"),
        )
        if not terminal_available:
            # Model the unavailable capability before the first epoch installs
            # its surface; availability never mutates an installed prefix.
            for name in ("terminal", "terminal_process", "terminal_monitor"):
                port._tools.pop(name, None)
        seal_test_direct_tool_port(port)
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=test_model_resolution_snapshot,
            repository=repository,
            writer_lease=lease,
            model=model,
            tools=port,
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
        )
        try:
            result = await runner.run_turn(
                frozen_test_prompt("export the saved result and inspect its file")
            )
            assert result.final_text == "source checked"
        finally:
            await port.aclose()

    asyncio.run(run())
    assert (tmp_path / "result.txt").read_bytes() == b"exact source"
    bodies = [
        json.loads(part.text)["pulsara_tool_result"]
        for message in model.requests[-1].compiled_input.messages
        if message.role is MessageRole.TOOL_RESULT
        for part in message.content
        if isinstance(part, LLMTextPart)
    ]
    exported = json.loads(bodies[0]["body"])
    assert exported["path"] == str(tmp_path / "result.txt")
    assert exported["source_coverage"] == "COMPLETE"
    assert bodies[0]["model_visible_memory_ids"] == []
    assert "exact source" in bodies[1]["body"]
    for before, after in zip(model.requests, model.requests[1:]):
        assert after.compiled_input.tools == before.compiled_input.tools
        assert after.compiled_input.system_prompt == before.compiled_input.system_prompt
        assert (
            after.compiled_input.messages[: len(before.compiled_input.messages)]
            == before.compiled_input.messages
        )
