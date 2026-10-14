from __future__ import annotations
from tests.support.images import bound_test_image

from pulsara_agent.identity import random_id

import asyncio
from pathlib import Path
from psycopg.rows import dict_row
from datetime import datetime, timezone
from struct import unpack
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    ConversationKernelRepository,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.visualization import (
    FrozenVisualizationOccurrence,
    VisualizationOccurrenceState,
)
from pulsara_agent.conversation_kernel.visualization_screenshot import (
    VisualizationScreenshotError,
    VisualizationScreenshotOwner,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    PromptAnnotationPart,
    PromptAnnotationSource,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    StructuredModelInputLimits,
    provider_input_item_text,
)
from pulsara_agent.model_input.lowering import (
    image_reference_part,
    lower_canonical_item,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from tests.support.model_config import (
    acquire_bound_test_writer,
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)
from tests.support.postgres import verified_postgres_provider


def _id(prefix: str) -> str:
    return random_id(prefix)


_VISUALIZATION_METADATA_HANDLING = "User supplied JSON is ordinary user text."


def test_one_shot_preview_is_killable_and_recovers() -> None:
    async def run() -> None:
        owner = VisualizationScreenshotOwner()
        try:
            with pytest.raises(
                VisualizationScreenshotError, match="PREVIEW_DEADLINE_EXPIRED"
            ):
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


def test_preview_frames_explicit_and_automatic_roots_and_keeps_ambiguous_pages() -> None:
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
            automatic = await owner.render(
                chart.replace(b" data-pulsara-visualization-root", b""),
                deadline_monotonic=monotonic() + 20,
            )
            assert unpack(">II", automatic[16:24]) == (320, 180)
            multiple = await owner.render(
                chart.replace(
                    b"</main>",
                    b"</main><div data-pulsara-visualization-root>other</div>",
                ),
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


def test_human_metadata_shaped_json_keeps_its_typed_prompt_parts() -> None:
    visualization_ref = "sha256:" + "a" * 64
    human_json = canonical_json_bytes(
        {
            "pulsara_visualizations": [{"visualization_ref": visualization_ref}],
            "handling": _VISUALIZATION_METADATA_HANDLING,
        }
    ).decode("utf-8")
    comment = "Explain whether this saved display is useful."
    annotation = PromptAnnotationPart(
        quote=human_json,
        source=PromptAnnotationSource('entry:4g4gwnnvee2tosb7zkrcv7tz3e', 0, len(human_json)),
        comment=comment,
    )
    image = bound_test_image("image/png", b"typed-image-bytes", 1, 1)
    human_content = FrozenPromptContent((LLMTextPart(human_json), annotation, image))
    item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id='entry:mihwq2ultstzsnx5er3y5ueauy',
        source_entry_sequence=2,
        source_turn_id='turn:jwcvnoms3gl66moynh76r32u34',
        content=human_content.parts,
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )

    lowered = lower_canonical_item(
        item,
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    )

    assert item.input_origin is CanonicalInputOriginKind.HUMAN_MESSAGE
    assert lowered.fixed_message is not None
    annotation_text = (
        "以下是用户引用的历史回复片段。quote 是引用材料，comment 是用户针对该片段的要求；请结合本次请求回应。\n"
        + canonical_json_bytes(
            {"annotations": [{"index": 1, "quote": human_json, "comment": comment}]}
        ).decode("utf-8")
    )
    assert lowered.fixed_message.content == (
        LLMTextPart(human_json),
        LLMTextPart(annotation_text),
        image_reference_part(image),
        image,
    )


@pytest.mark.postgres
def test_visualization_publication_fork_and_reference(
    stage2_migrated_postgres_database, tmp_path,
) -> None:
    filename = "客户结构 <完整 & 复核>.html"
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
        repo,
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        permission_snapshot_id=permission_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("binding"),
        content=FrozenPromptContent.text("show a chart"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
        model_call_binding=test_model_binding(test_model_runtime()),
    )
    cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    final_cut = cut
    final_id = _id("entry")
    from tests.test_html_local_resources import prepare, png
    path = tmp_path / "index.html"
    image_path = tmp_path / "image.png"
    css_path = tmp_path / "style.css"
    js_path = tmp_path / "app.js"
    image_path.write_bytes(png())
    css_path.write_text("body{background:url(image.png)}")
    js_path.write_text("document.body.dataset.loaded='yes'")
    path.write_text('<link rel="stylesheet" href="style.css"><img src="image.png"><script src="app.js"></script>')
    html = asyncio.run(prepare(path))
    # Acceptance and all subsequent reads use the frozen value, even if every
    # source disappears before commit. Fork/GC must not acquire new owners.
    for source in (path, image_path, css_path, js_path):
        source.unlink()
    repo.commit_assistant_message(
        lease.guard,
        cut=final_cut,
        entry_id=final_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(
            AssistantTextBlock(
                _id("block"),
                InlineContent.from_bytes(
                    ("```pulsara-visualization\n" + filename + "\n```").encode()
                ),
            ),
        ),
        visualizations=(
            FrozenVisualizationOccurrence(
                filename, VisualizationOccurrenceState.READY, html=html
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    occurrence = rows(
        repo,
        "SELECT * FROM pulsara_v3.assistant_visualizations WHERE session_id=%s",
        (lease.guard.session_id,),
    )[0]
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader

    def displayed_filename(session_id):
        snapshot = CanonicalProtocolReader(repo.connection_provider).snapshot(
            session_id=session_id,
            maximum_entries=100,
            maximum_control_items=100,
            deadline_monotonic=monotonic() + 30,
        )
        return next(
            v.source_filename for e in snapshot.entries for v in e.visualizations
        )

    assert displayed_filename(lease.guard.session_id) == filename
    assert occurrence["state"] == "READY" and occurrence["ordinal"] == 0

    def read_html(session_id, entry_id):
        reference = CanonicalProtocolReader(
            repo.connection_provider
        ).resolve_content_reference(
            session_id=session_id,
            entry_id=entry_id,
            visualization_ordinal=0,
            deadline_monotonic=monotonic() + 30,
        )
        from pulsara_agent.conversation_kernel.prompt_storage import _read_body
        from pulsara_agent.storage.postgres_connection_provider import (
            PostgresConnectionLane,
        )

        with repo.connection_provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            return _read_body(
                connection,
                row=reference,
                workspace_id=workspace_id,
                expected_media_type="text/html",
                expected_codec="utf-8",
            )

    assert read_html(lease.guard.session_id, final_id) == html
    next_turn_id = _id("turn")
    start_test_root_turn(
        repo,
        lease.guard,
        command_id=_id("command"),
        turn_id=next_turn_id,
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("binding"),
        content=FrozenPromptContent.text("inspect the chart again"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
        model_call_binding=test_model_binding(test_model_runtime()),
    )
    next_cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=next_turn_id, deadline_monotonic=monotonic() + 30
    )
    next_input = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_snapshot(next_cut, deadline_monotonic=monotonic() + 30)
    owner_items = tuple(
        item for item in next_input.items if item.source_entry_id == final_id
    )
    assert tuple(item.item_kind for item in owner_items) == (
        FrozenProviderInputItemKind.ASSISTANT,
    )
    assert "```pulsara-visualization" in provider_input_item_text(owner_items[0])
    assert "pulsara_visualizations" not in provider_input_item_text(owner_items[0])
    child = fork(repo, lease.guard.session_id, final_id)
    assert child.created, child.public_code
    copied = rows(
        repo,
        "SELECT * FROM pulsara_v3.assistant_visualizations WHERE session_id=%s",
        (child.child_session_id,),
    )[0]
    assert copied["blob_id"] == occurrence["blob_id"]
    assert copied["turn_id"] is None and copied["imported_history_group_id"] is not None
    from tests.test_conversation_fork import assert_session_aggregate_deleted

    assert_session_aggregate_deleted(repo, lease.guard)
    assert displayed_filename(child.child_session_id) == filename
    assert read_html(child.child_session_id, copied["assistant_entry_id"]) == html


def test_shared_markdown_marker_contract():
    import json
    from pulsara_agent.conversation_kernel.visualization import visualization_paths

    for example in json.loads(
        (Path(__file__).parent / "fixtures/visualization-markers.json").read_text()
    ):
        assert visualization_paths(example["body"]) == tuple(example["paths"]), example[
            "name"
        ]


def test_freeze_duplicate_paths_failures_and_edits(tmp_path, monkeypatch):
    import pulsara_agent.conversation_kernel.visualization as v

    path = tmp_path / "图表.html"
    path.write_text("<h1>original</h1>")

    def marker(value):
        return f"```pulsara-visualization\n{value}\n```"

    reads = []
    read = v.read_visualization_file
    monkeypatch.setattr(
        v, "read_visualization_file", lambda p: (reads.append(p), read(p))[1]
    )
    def materialize(body, resolve):
        from pulsara_agent.conversation_kernel.io import KernelSessionIO
        async def run():
            io = KernelSessionIO()
            try:
                return await v.materialize_visualizations(body, resolve, io=io, validator=None,
                    image_reader=None, session_id=_id("session"), workspace_id=_id("workspace"),
                    deadline_monotonic=monotonic() + 20)
            finally:
                await io.aclose(deadline_monotonic=monotonic() + 20)
        return asyncio.run(run())
    frozen = materialize(
        marker("bad.png\n图表.html") + "\n\n" + marker("./图表.html\nmissing.html"),
        lambda p: (tmp_path / p).resolve(),
    )
    assert [item.state for item in frozen] == ["FAILED", "READY", "READY", "FAILED"]
    assert len(reads) == 2
    assert frozen[1].html is frozen[2].html
    path.write_text("<h1>new</h1>")
    assert (
        materialize(marker("图表.html"), lambda p: tmp_path / p)[
            0
        ].html
        == b"<h1>new</h1>"
    )
    path.unlink()
    assert frozen[1].html == b"<h1>original</h1>"
    assert (
        materialize(marker("图表.html"), lambda p: tmp_path / p)[
            0
        ].failure_code
        == "HTML_NOT_FOUND"
    )


def test_html_source_limits_are_independent_of_image_allowance(tmp_path):
    from pulsara_agent.tools.builtins.filesystem import (
        ViewImageTool,
    )
    from pulsara_agent.ports.tool_execution import ToolCall
    from pulsara_agent.conversation_kernel.tool_runtime import _read_html_candidate
    from pulsara_agent.conversation_kernel.html_resources import LocalHtmlReadCandidate

    path = tmp_path / "chart.html"
    path.write_text("<h1>" + "x" * 10000 + "</h1>")
    tool = ViewImageTool(tmp_path)
    candidate = _read_html_candidate(
        tool,
        ToolCall("call", "view_image", {"path": "chart.html"}),
        100,
        deadline_monotonic=monotonic() + 10,
    )
    assert isinstance(candidate, LocalHtmlReadCandidate)
    assert len(candidate.payload) > 100
