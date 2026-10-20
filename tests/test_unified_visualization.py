"""Frozen unified visualization contract: publication, shared reads and recovery."""

import asyncio
from datetime import datetime, timezone
from dataclasses import replace
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    ConversationKernelConflict,
)
from pulsara_agent.conversation_kernel.visualization import (
    FrozenVisualizationOccurrence,
    VisualizationOccurrenceState,
    materialize_visualizations,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    PostgresCanonicalImageReferenceReadPort,
    CanonicalImageReferenceUnavailable,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.conversation_kernel.session_content import (
    SessionContentQuery,
)
from pulsara_agent.conversation_kernel.history_projection import (
    canonical_history_message,
)
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.test_kernel_image_input_k2_postgres import (
    _repository,
    _bound_session,
    _workspace,
    _image,
    _png,
    _direct_intent,
    _rows,
)
from tests.test_conversation_fork import assert_session_aggregate_deleted, fork


def test_async_preparation_mixed_sources_neutrality_and_timeout(tmp_path):
    async def run():
        io = KernelSessionIO()
        validator = HostPromptImageValidator()
        workspace = random_id("workspace")
        image = _image(workspace_id=workspace)

        class Reader:
            def read_image(self, **kwargs):
                assert kwargs["workspace_id"] == workspace
                return image

        (tmp_path / "chart.html").write_text("<h1>chart</h1>")
        (tmp_path / "plot.png").write_bytes(_png())

        async def prepare(body, **kwargs):
            return await materialize_visualizations(
                body,
                lambda x: (tmp_path / x).resolve(),
                io=io,
                validator=validator,
                image_reader=Reader(),
                session_id=random_id("session"),
                workspace_id=workspace,
                deadline_monotonic=monotonic() + 20,
                **kwargs,
            )

        try:
            result = await prepare(
                f"```pulsara-visualization\nchart.html\nplot.png\n{image.image_ref}\n./plot.png\nmissing.png\nsha256:abcd\nimage:abcd\n```"
            )
            assert [x.state for x in result] == ["READY"] * 4 + ["FAILED"] * 3
            assert result[0].html == b"<h1>chart</h1>"
            assert result[1].image == result[2].image == result[3].image == image
            assert result[1].image is result[3].image
            (tmp_path / "plot.png").unlink()
            assert result[1].image.immutable_bytes == _png()

            # Classify the requested path, not the symlink target. A non-HTML
            # alias must not inherit an HTML result from the preparation cache.
            (tmp_path / "document").write_text("<h1>linked</h1>")
            (tmp_path / "linked.html").symlink_to(tmp_path / "document")
            (tmp_path / "alias.png").symlink_to(tmp_path / "chart.html")
            linked = await prepare(
                "```pulsara-visualization\nlinked.html\nchart.html\nalias.png\n```"
            )
            assert linked[0].html == b"<h1>linked</h1>"
            assert linked[1].html == b"<h1>chart</h1>"
            assert linked[2].state is VisualizationOccurrenceState.FAILED

            # SVG has a code-fence/HTML route, not a raw image-file publication
            # route. The separate view_image SVG reader remains supported.
            svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20"/></svg>'
            (tmp_path / "diagram.svg").write_text(svg)
            (tmp_path / "diagram.html").write_text(f"<main>{svg}</main>")
            vectors = await prepare(
                "```pulsara-visualization\ndiagram.svg\ndiagram.html\n```"
            )
            assert vectors[0].state is VisualizationOccurrenceState.FAILED
            assert vectors[0].failure_code == "IMAGE_DECODE_FAILED"
            assert vectors[1].state is VisualizationOccurrenceState.READY
            assert b"<svg" in vectors[1].html
            assert await prepare(f"```svg\n{svg}\n```") == ()

            # A logical watchdog is never an ordinary missing-file item.
            class TimedIO:
                async def run(self, *args, **kwargs):
                    raise TimeoutError("watchdog")

            with pytest.raises(TimeoutError, match="watchdog"):
                await materialize_visualizations(
                    "```pulsara-visualization\nplot.png\n```",
                    lambda x: tmp_path / x,
                    io=TimedIO(),
                    validator=validator,
                    image_reader=None,
                    session_id=random_id("session"),
                    workspace_id=workspace,
                    deadline_monotonic=monotonic() + 20,
                )
        finally:
            await validator.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 20)

    asyncio.run(run())


@pytest.mark.postgres
def test_shared_publication_history_chunks_fork_and_gc(
    stage2_migrated_postgres_database, monkeypatch
):
    repo = _repository(stage2_migrated_postgres_database)
    a, aruntime, _ = _bound_session(repo)
    workspace = _workspace(repo, a)
    image = _image(workspace_id=workspace)
    _direct_intent(repo, a, aruntime, FrozenPromptContent((image,)))
    b, bruntime, _ = _bound_session(repo, workspace_id=workspace)
    intent, _, _ = _direct_intent(
        repo, b, bruntime, FrozenPromptContent.text("show saved image")
    )
    c, _, _ = _bound_session(repo)
    d, _, _ = _bound_session(repo, workspace_id=workspace)

    def reader(lease):
        return PostgresCanonicalImageReferenceReadPort(
            repo.connection_provider,
            session_id=lease.guard.session_id,
            workspace_id=_workspace(repo, lease),
        )

    b_reader = reader(b)

    def read(port, lease, ws=workspace):
        return port.read_image(
            session_id=lease.guard.session_id,
            workspace_id=ws,
            image_ref=image.image_ref,
            maximum_encoded_bytes=1 << 20,
            deadline_monotonic=monotonic() + 30,
        )

    assert read(b_reader, b) == image  # B does not own A's image.
    with pytest.raises(CanonicalImageReferenceUnavailable):
        read(reader(c), c, _workspace(repo, c))
    cut = repo.prepare_provider_input_cut(
        b.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30
    )
    entry = random_id("entry")
    candidates = (
        FrozenVisualizationOccurrence(
            "chart.html", VisualizationOccurrenceState.READY, html=b"<h1>chart</h1>"
        ),
        FrozenVisualizationOccurrence(
            "图片", VisualizationOccurrenceState.READY, image=image
        ),
        FrozenVisualizationOccurrence(
            "again.png", VisualizationOccurrenceState.READY, image=image
        ),
        FrozenVisualizationOccurrence(
            "missing.png",
            VisualizationOccurrenceState.FAILED,
            failure_code="IMAGE_READ_FAILED",
            failure_detail="图片不可用",
        ),
    )
    submission = dict(
        cut=cut,
        entry_id=entry,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(
            AssistantTextBlock(
                random_id("block"), InlineContent.from_bytes(b"Here is the image.")
            ),
        ),
        visualizations=candidates,
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    # A forced image identity collision rolls back the entire gallery, including
    # the preceding HTML occurrence and assistant message, without changing A.
    from pulsara_agent.conversation_kernel import blob as blob_module
    original_id = blob_module._blob_id
    conflicting = replace(image, immutable_bytes=_png(size=(8, 6)), width=8, height=6)
    with monkeypatch.context() as patch:
        patch.setattr(
            blob_module, "_blob_id",
            lambda *args, **kwargs: image.image_ref
            if kwargs.get("image") else original_id(*args, **kwargs),
        )
        with pytest.raises(ConversationKernelConflict, match="IDENTITY_CONFLICT"):
            repo.commit_assistant_message(
                b.guard, **{**submission, "visualizations": (
                    candidates[0], replace(candidates[1], image=conflicting),
                )},
            )
    assert not _rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE id=%s", (entry,))
    assert not _rows(repo, "SELECT ordinal FROM pulsara_v3.assistant_visualizations WHERE assistant_entry_id=%s", (entry,))
    assert read(b_reader, b) == image
    repo.commit_assistant_message(b.guard, **submission)
    assert repo.confirm_assistant_message_winner(b.guard, **submission) is not None
    drifted = replace(candidates[1], image=replace(image, width=image.width + 1))
    with pytest.raises(ConversationKernelConflict):
        repo.confirm_assistant_message_winner(
            b.guard,
            **{
                **submission,
                "visualizations": (candidates[0], drifted, *candidates[2:]),
            },
        )
    protocol = CanonicalProtocolReader(repo.connection_provider)
    snapshot = protocol.snapshot(
        session_id=b.guard.session_id,
        maximum_entries=100,
        maximum_control_items=100,
        deadline_monotonic=monotonic() + 30,
    )
    saved = next(e for e in snapshot.entries if e.entry_id == entry).visualizations
    assert [x.state for x in saved] == ["READY", "READY", "READY", "FAILED"]
    assert (
        saved[1].image_ref == image.image_ref
        and saved[1].width == 7
        and saved[1].codec == "binary"
    )
    assert not saved[0].HasField("image_ref") and saved[0].media_type == "text/html"
    assert_session_aggregate_deleted(repo, a.guard)
    assert read(b_reader, b) == image  # Visualization alone now keeps it readable.
    # History reads canonical metadata only, and generated references are not lexical fields.
    session = _rows(
        repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (d.guard.session_id,)
    )[0]
    query = SessionContentQuery(
        repo,
        session_id=d.guard.session_id,
        memory_domain_id=session["memory_domain_id"],
    )

    def history(**kwargs):
        return query.invoke(
            "read_session_content",
            {"session_id": b.guard.session_id, "entry_id": entry, "limit": 1, **kwargs},
            current_range=SessionContentRange(0),
            tool_call_id="call:history",
            deadline_monotonic=monotonic() + 30,
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            PostgresCanonicalImageReferenceReadPort,
            "read_image",
            lambda *a, **k: pytest.fail("history hydrated image"),
        )
        text = history()["items"][0]["text"]
        assert (
            text.count(image.image_ref) == 2
            and "已保存展示项 2" in text
            and "已保存展示项 3" in text
        )
        first = history(max_chars=len("Here is the image.\n"))
        assert image.image_ref not in str(first["items"]) and first["next_cursor"]
        with repo.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as conn:
            row = conn.execute(
                "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s", (entry,)
            ).fetchone()
            projected = canonical_history_message(conn, row)
            assert "已保存展示项" in projected["text"]
            assert image.image_ref not in str(projected["fields"])
            assert "已保存展示项" not in str(projected["fields"])

    async def chunk(lease, offset, **fields):
        request = wire.ReadContentRequest(
            request_id="request:test",
            image_ref=image.image_ref,
            offset_bytes=offset,
            limit_bytes=20,
            **fields,
        )
        return await TerminalKernelProtocolServer.read_canonical_content(
            connection_provider=repo.connection_provider,
            protocol_reader=protocol,
            session_id=lease.guard.session_id,
            request=request,
        )

    first = asyncio.run(chunk(d, 0))
    assert (
        first.content.image_ref == image.image_ref
        and first.content.width == image.width
    )
    assert first.content.content == image.immutable_bytes[:20]
    invalid = asyncio.run(chunk(d, 0, visualization_ordinal=1))
    assert invalid.error.stable_code == "CONTENT_TARGET_INVALID"
    child = fork(repo, b.guard.session_id, entry)
    assert child.created
    child_entry = _rows(
        repo,
        "SELECT assistant_entry_id FROM pulsara_v3.assistant_visualizations WHERE session_id=%s AND ordinal=1",
        (child.child_session_id,),
    )[0]["assistant_entry_id"]
    assert (
        protocol.resolve_content_reference(
            session_id=child.child_session_id,
            entry_id=child_entry,
            visualization_ordinal=1,
            deadline_monotonic=monotonic() + 30,
        )["image_ref"]
        == image.image_ref
    )
    assert_session_aggregate_deleted(repo, b.guard)
    with pytest.raises(CanonicalImageReferenceUnavailable):
        read(b_reader, b)  # Stale caller cannot use child owner.
    assert read(reader(d), d) == image
    # Removing the remaining owner leaves neither a readable orphan nor a UI pin.
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.ARTIFACT,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as conn:
        conn.execute(
            "DELETE FROM pulsara_v3.sessions WHERE id=%s", (child.child_session_id,)
        )
    second = asyncio.run(chunk(d, 20))
    assert second.error.stable_code == "CONTENT_REFERENCE_MISSING"
    with pytest.raises(CanonicalImageReferenceUnavailable):
        read(reader(d), d)


def test_cold_image_content_target_is_exclusive():
    from types import SimpleNamespace
    from pulsara_agent.desktop_app.http_server import LocalHttpServer

    calls = []

    async def history(session, request, **kwargs):
        calls.append((session, request))
        return wire.ServerFrame(content=wire.CanonicalContentChunk(content=b"image"))

    server = SimpleNamespace(sessions=SimpleNamespace(history_content=history))
    image_ref = "image:" + "a" * 26
    result = asyncio.run(
        LocalHttpServer._cold_content(
            server, "session:caller", {"image_ref": image_ref}
        )
    )
    assert result["content"]["content"]
    assert calls[0][1].WhichOneof("target") == "image_ref"
    for extra in (
        {"entry_id": "entry:other"},
        {"queue_item_id": "queue:other"},
        {"image_ref_ordinal": 0},
        {"visualization_ordinal": 0},
        {"block_id": ""},
    ):
        with pytest.raises(ValueError):
            asyncio.run(
                LocalHttpServer._cold_content(
                    server, "session:caller", {"image_ref": image_ref, **extra}
                )
            )
    assert len(calls) == 1
