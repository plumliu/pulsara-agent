from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from struct import unpack
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptors
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.visualization import (
    FrozenVisualizationOccurrence,
    PostgresCanonicalVisualizationReadPort,
    VisualizationOccurrenceState,
    VisualizationSource,
    VisualizationSourceKind,
    VisualizationSubscription,
    materialize_visualization_subscription,
    parse_visualization_source,
)
from pulsara_agent.conversation_kernel.visualization_screenshot import (
    VisualizationScreenshotError,
    VisualizationScreenshotOwner,
)
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.ports.artifact import ToolOutputArtifactDisposition, ToolResultDisplayKind
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.support.model_config import (
    acquire_bound_test_writer,
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)
from tests.support.postgres import verified_postgres_provider


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def test_subscription_source_and_publication_read_boundaries(tmp_path: Path) -> None:
    source, review = parse_visualization_source({"path": "demo.html"})
    assert source.kind is VisualizationSourceKind.PATH and not review
    assert not (tmp_path / "demo.html").exists()
    subscription = VisualizationSubscription("turn", "result", source)

    class Reader:
        def read_ref(self, reference: str, *, deadline_monotonic: float) -> bytes:
            raise AssertionError("path source must not read a reference")

    assert materialize_visualization_subscription(
        VisualizationSubscription(
            subscription.turn_id,
            subscription.source_result_entry_id,
            VisualizationSource(VisualizationSourceKind.PATH, str(tmp_path / "demo.html")),
        ),
        Reader(),  # type: ignore[arg-type]
        deadline_monotonic=monotonic() + 10,
    ) is None
    (tmp_path / "demo.html").write_text("<html>final</html>", encoding="utf-8")
    result = materialize_visualization_subscription(
        VisualizationSubscription(
            "turn", "result",
            VisualizationSource(VisualizationSourceKind.PATH, str(tmp_path / "demo.html")),
        ),
        Reader(),  # type: ignore[arg-type]
        deadline_monotonic=monotonic() + 10,
    )
    assert result == FrozenVisualizationOccurrence(
        "result", VisualizationOccurrenceState.READY, html=b"<html>final</html>"
    )
    (tmp_path / "demo.html").write_bytes(b"\xff")
    failure = materialize_visualization_subscription(
        VisualizationSubscription(
            "turn", "result",
            VisualizationSource(VisualizationSourceKind.PATH, str(tmp_path / "demo.html")),
        ),
        Reader(),  # type: ignore[arg-type]
        deadline_monotonic=monotonic() + 10,
    )
    assert failure is not None and failure.failure_code == "HTML_NOT_UTF8"


def test_one_shot_preview_is_killable_and_recovers() -> None:
    async def run() -> None:
        owner = VisualizationScreenshotOwner()
        try:
            with pytest.raises(VisualizationScreenshotError, match="PREVIEW_DEADLINE_EXPIRED"):
                await owner.render(
                    b"<html><script>while(true){}</script></html>",
                    deadline_monotonic=monotonic() + 1.5,
                )
            screenshot = await owner.render(
                b"<!doctype html><html><body><h1>Chart</h1></body></html>",
                deadline_monotonic=monotonic() + 20,
            )
            assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
        finally:
            await owner.aclose()

    asyncio.run(run())


def test_preview_frames_one_marked_root_and_keeps_unmarked_pages() -> None:
    async def run() -> None:
        owner = VisualizationScreenshotOwner()
        try:
            chart = (
                b"<!doctype html><html><head><style>"
                b"body{margin:0;min-height:100vh;display:grid;place-items:center;background:#111}"
                b"main{width:320px;height:180px;background:#abc}</style></head><body>"
                b"<main data-pulsara-visualization-root>chart</main></body></html>"
            )
            cropped = await owner.render(chart, deadline_monotonic=monotonic() + 20)
            assert unpack(">II", cropped[16:24]) == (320, 180)
            whole_page = await owner.render(
                chart.replace(b" data-pulsara-visualization-root", b""),
                deadline_monotonic=monotonic() + 20,
            )
            assert unpack(">II", whole_page[16:24]) == (1200, 800)
            multiple = await owner.render(
                chart.replace(b"</main>", b"</main><div data-pulsara-visualization-root>other</div>"),
                deadline_monotonic=monotonic() + 20,
            )
            assert unpack(">II", multiple[16:24]) == (1200, 800)
            hidden = await owner.render(
                chart.replace(b"main{width:", b"main{visibility:hidden;width:"),
                deadline_monotonic=monotonic() + 20,
            )
            assert unpack(">II", hidden[16:24]) == (1200, 800)
        finally:
            await owner.aclose()

    asyncio.run(run())


def test_visualization_tool_explains_authoring_and_each_path() -> None:
    tool = next(item for item in builtin_tool_descriptors() if item.name == "visualization_render")
    assert "data-pulsara-visualization-root" in tool.description
    assert "whole website/page demo" in tool.description
    assert "does not read the file yet" in tool.description
    assert "fails" in tool.description
    assert tool.input_schema is not None
    properties = tool.input_schema["properties"]
    assert isinstance(properties, dict)
    for name in ("path", "visualization_ref", "review"):
        assert properties[name]["description"]
    assert "deletion" in properties["path"]["description"]
    assert "Do not invent" in properties["visualization_ref"]["description"]
    assert "display remains scheduled" in properties["review"]["description"]


@pytest.mark.postgres
def test_visualization_publication_fork_and_reference(
    stage2_migrated_postgres_database,
) -> None:
    from tests.test_conversation_fork import fork, rows

    repo = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    workspace_id = _id("workspace")
    lease = acquire_bound_test_writer(
        repo,
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=120,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _id("turn")
    permission_id = _id("permission")
    start_test_root_turn(
        repo, lease.guard,
        command_id=_id("command"), turn_id=turn_id,
        permission_snapshot_id=permission_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        entry_id=_id("entry"), context_binding_revision_id=_id("revision"),
        content=FrozenPromptContent.text("show a chart"),
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic() + 30,
        model_call_binding=test_model_binding(test_model_runtime()),
    )
    cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    request_id, call_id = _id("entry"), _id("call")
    repo.commit_assistant_message(
        lease.guard, cut=cut, entry_id=request_id,
        parent_content=InlineContent.from_bytes(b""),
        blocks=(AssistantToolCallBlock(
            _id("block"), call_id, "visualization_render",
            freeze_json({"path": "chart.html", "review": False}),
        ),),
        occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    from pulsara_agent.primitives.run_permission import (
        RunPermissionAdmissionSource, build_run_permission_snapshot,
    )
    attempt = repo.accept_tool_attempt(
        lease.guard, attempt_id=_id("attempt"), assistant_entry_id=request_id,
        tool_call_id=call_id, authorization_kind="policy",
        authorization_reference="allow", actor_kind="runtime", actor_id="tool",
        remote_idempotency_key=None, retry_of_attempt_id=None,
        permission_snapshot_fingerprint=build_run_permission_snapshot(
            snapshot_id=permission_id, requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        ).snapshot_fingerprint,
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic() + 30,
    )
    result_id = _id("entry")
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard, workspace_id=workspace_id,
        result_id=_id("result"), result_entry_id=result_id,
        turn_id=turn_id, assistant_entry_id=request_id, tool_call_id=call_id,
        attempt_id=attempt.attempt_id, result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"Visualization is subscribed."),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None, artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None, artifact_unavailability_reason=None,
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="visualization_render",
    )
    repo.accept_tool_result(lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30)
    final_cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    final_id = _id("entry")
    html = b"<!doctype html><html><body>canonical chart</body></html>"
    repo.commit_assistant_message(
        lease.guard, cut=final_cut, entry_id=final_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(AssistantTextBlock(_id("block"), InlineContent.from_bytes(b"Chart ready")),),
        visualizations=(FrozenVisualizationOccurrence(
            result_id, VisualizationOccurrenceState.READY, html=html
        ),),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    occurrence = rows(repo, "SELECT * FROM pulsara_v3.assistant_visualizations WHERE session_id=%s", (lease.guard.session_id,))[0]
    assert occurrence["state"] == "READY" and occurrence["ordinal"] == 0
    digest = rows(repo, "SELECT logical_digest FROM pulsara_v3.blobs WHERE id=%s", (occurrence["blob_id"],))[0]["logical_digest"]
    assert PostgresCanonicalVisualizationReadPort(
        repo.connection_provider, session_id=lease.guard.session_id,
        workspace_id=workspace_id,
    ).read_ref(str(digest), deadline_monotonic=monotonic() + 30) == html
    child = fork(repo, lease.guard.session_id, final_id)
    assert child.created, child.public_code
    copied = rows(repo, "SELECT * FROM pulsara_v3.assistant_visualizations WHERE session_id=%s", (child.child_session_id,))[0]
    assert copied["blob_id"] == occurrence["blob_id"]
    assert copied["turn_id"] is None and copied["imported_history_group_id"] is not None
    assert PostgresCanonicalVisualizationReadPort(
        repo.connection_provider, session_id=child.child_session_id,
        workspace_id=workspace_id,
    ).read_ref(str(digest), deadline_monotonic=monotonic() + 30) == html
