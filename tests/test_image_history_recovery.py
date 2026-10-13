"""Image history views use canonical metadata, atomic pages and existing authority."""

from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.history_projection import (
    canonical_history_message,
    rebuild_history_projection,
)
from pulsara_agent.conversation_kernel.history_excerpt import (
    HistoryCandidate,
    excerpt_history,
    _weights,
)
from pulsara_agent.conversation_kernel.session_content import (
    SessionContentQuery,
    SessionQueryError,
    _encode,
    _decode,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    PostgresCanonicalImageReferenceReadPort,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    HISTORICAL_IMAGE_MARKER,
    PromptAnnotationPart,
    PromptAnnotationSource,
)
from pulsara_agent.model_input.lowering import image_omission_part
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_conversation_fork import final, rows
from tests.test_kernel_image_input_k2_postgres import (
    _repository,
    _bound_session,
    _direct_intent,
    _image,
)


@pytest.mark.postgres
def test_canonical_image_history_atomic_pages_search_and_cross_session(
    stage2_migrated_postgres_database, monkeypatch
):
    repo = _repository(stage2_migrated_postgres_database)
    lease, runtime, _ = _bound_session(repo)
    image = _image()
    placeholder = image_omission_part(image).text
    originals = [
        FrozenPromptContent(
            (LLMTextPart("needle-left"), image, LLMTextPart("右侧"), image)
        ),
        FrozenPromptContent((image, image)),
    ]
    entries = []
    for content in originals:
        intent, _, _ = _direct_intent(repo, lease, runtime, content)
        final(repo, lease.guard, intent.turn_id, "saved answer needle-answer")
        entries.append(intent.entry_id)
    session = rows(
        repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (lease.guard.session_id,)
    )[0]
    query = SessionContentQuery(
        repo, session_id=session["id"], memory_domain_id=session["memory_domain_id"]
    )
    scope = SessionContentRange(session["latest_entry_sequence"])

    def read(**arguments):
        return query.invoke(
            "read_session_content",
            arguments,
            current_range=scope,
            tool_call_id="call:history",
            deadline_monotonic=monotonic() + 30,
        )

    # Rendering must never invoke payload hydration, image decoding or the visual port.
    def forbid(*args, **kwargs):
        raise AssertionError("history metadata view attempted image payload I/O")

    monkeypatch.setattr(PostgresCanonicalImageReferenceReadPort, "read_image", forbid)
    from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore

    original_read = PostgresCanonicalBlobStore.read_exact_in_connection

    def metadata_only(connection, **kwargs):
        assert not kwargs.get("expected_media_type", "").startswith("image/")
        return original_read(connection, **kwargs)

    monkeypatch.setattr(
        PostgresCanonicalBlobStore, "read_exact_in_connection", metadata_only
    )
    page = read(
        entry_id=entries[0],
        limit=1,
        max_chars=len("needle-left\n") + len(placeholder) - 1,
    )
    assert page["items"][0]["text"] == "needle-left\n"
    cursor = page["next_cursor"]
    chunks = [page["items"][0]["text"]]
    while cursor:
        page = read(cursor=cursor, limit=1, max_chars=len(placeholder))
        if page["items"][0]["entry_id"] != entries[0]:
            break
        chunk = page["items"][0]["text"]
        assert not ("image_ref:" in chunk and placeholder not in chunk)
        chunks.append(chunk)
        cursor = page["next_cursor"]
    assert "".join(chunks) == f"needle-left\n{placeholder}\n右侧\n{placeholder}"
    with pytest.raises(SessionQueryError, match="Increase max_chars") as exc:
        read(entry_id=entries[1], limit=1, max_chars=len(placeholder) - 1)
    assert exc.value.code == "RESULT_BUDGET_EXCEEDED"
    first = read(entry_id=entries[1], limit=1, max_chars=len(placeholder))
    assert first["items"][0]["text"] == placeholder
    old = _decode(first["next_cursor"], "read_session_content")
    old["operation"] = "read_session_content.v2"
    with pytest.raises(SessionQueryError) as exc:
        read(cursor=_encode(old))
    assert exc.value.code == "CURSOR_INVALID"
    old["operation"] = "read_session_content.v3"
    old["offset"] = 1
    with pytest.raises(SessionQueryError, match="splits an image"):
        read(cursor=_encode(old))
    # Existing installed range/exclusion remain authoritative, including explicit self.
    assert (
        query.invoke(
            "read_session_content",
            {"session_id": session["id"]},
            current_range=SessionContentRange(0),
            tool_call_id="call:h",
            deadline_monotonic=monotonic() + 30,
        )["items"]
        == []
    )
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s", (entries[0],)
        ).fetchone()
        projected = canonical_history_message(connection, row)
        assert (
            projected["text"]
            == f"needle-left\n{HISTORICAL_IMAGE_MARKER}\n右侧\n{HISTORICAL_IMAGE_MARKER}"
        )
        assert [field["text"] for field in projected["fields"]] == [
            "needle-left",
            "右侧",
        ]
        assert image.content_digest not in str(projected)
        # Generated labels are packaging, so only the two real text parts
        # consume excerpt body quota (weighted bytes, before token rounding).
        assert sum(_weights(projected)) == len("needle-left") + 2 * 4
        rebuild_history_projection(connection, page_size=1)
    hits = query.invoke(
        "search_session_content",
        {"query": "needle-left"},
        current_range=scope,
        tool_call_id="call:h",
        deadline_monotonic=monotonic() + 30,
    )
    assert hits["items"] and HISTORICAL_IMAGE_MARKER in str(hits)
    assert image.content_digest not in str(hits)
    for term in (image.content_digest, "历史图片"):
        assert (
            query.invoke(
                "search_session_content",
                {"query": term},
                current_range=scope,
                tool_call_id="call:h",
                deadline_monotonic=monotonic() + 30,
            )["items"]
            == []
        )
    other, _, _ = _bound_session(repo)
    # Current default memory domain permits these sessions' historical text.
    cross = SessionContentQuery(
        repo,
        session_id=other.guard.session_id,
        memory_domain_id=session["memory_domain_id"],
    )
    page = cross.invoke(
        "read_session_content",
        {"session_id": session["id"], "entry_id": entries[1], "limit": 1},
        current_range=SessionContentRange(0),
        tool_call_id="call:h",
        deadline_monotonic=monotonic() + 30,
    )
    assert "未授予当前会话读图权限" in str(page)
    assert image.content_digest not in str(page)


@pytest.mark.postgres
def test_annotation_near_body_quota_remains_searchable_with_image(
    stage2_migrated_postgres_database,
):
    repo = _repository(stage2_migrated_postgres_database)
    lease, runtime, _ = _bound_session(repo)
    source_intent, _, _ = _direct_intent(
        repo, lease, runtime, FrozenPromptContent((LLMTextPart("source"),))
    )
    quote = "a" * 4056
    source_entry = final(repo, lease.guard, source_intent.turn_id, quote)
    intent, _, _ = _direct_intent(
        repo,
        lease,
        runtime,
        FrozenPromptContent(
            (
                PromptAnnotationPart(
                    quote=quote,
                    source=PromptAnnotationSource(source_entry, 0, len(quote)),
                    comment="needle ",
                ),
                _image(),
            )
        ),
    )
    final(repo, lease.guard, intent.turn_id, "saved answer")
    session = rows(
        repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (lease.guard.session_id,)
    )[0]
    query = SessionContentQuery(
        repo, session_id=session["id"], memory_domain_id=session["memory_domain_id"]
    )
    hits = query.invoke(
        "search_session_content",
        {"query": "needle"},
        current_range=SessionContentRange(session["latest_entry_sequence"]),
        tool_call_id="call:h",
        deadline_monotonic=monotonic() + 30,
    )
    annotation = next(
        message
        for hit in hits["items"]
        for message in hit["messages"]
        if message["entry_id"] == intent.entry_id
    )
    # The 1016-token annotation fits the 1024-token body quota. The image
    # label must not turn this real search hit into a discarded candidate.
    assert quote in annotation["text"]
    assert "needle " in annotation["text"]
    assert HISTORICAL_IMAGE_MARKER in annotation["text"]


def test_excerpt_image_markers_are_whole_and_bounded():
    chunk = HISTORICAL_IMAGE_MARKER + "\npayload "
    text = "needle " + chunk * 100
    start = len("needle ")
    message = {
        "entry_id": "entry:image",
        "sequence": 1,
        "text": text,
        "annotated": False,
        "region": "Q1",
        "body_ranges": [[0, start]]
        + [
            [
                start + i * len(chunk) + len(HISTORICAL_IMAGE_MARKER) + 1,
                start + (i + 1) * len(chunk),
            ]
            for i in range(100)
        ],
        "atomic_ranges": [
            [
                start + i * len(chunk),
                start + i * len(chunk) + len(HISTORICAL_IMAGE_MARKER),
            ]
            for i in range(100)
        ],
    }
    document = {
        "messages": [message],
        "body": "needle",
        "positions": [{"start": 0, "end": 6, "message": 0, "display_start": 0}],
    }
    result = excerpt_history(document, ([[0, 6]],), budget=30)
    assert result
    shown = result[0]["text"]
    assert len(shown) < len(text)
    assert "历史图片" not in shown.replace(HISTORICAL_IMAGE_MARKER, "")


@pytest.mark.parametrize("oversized", [False, True])
def test_search_and_recall_charge_image_packaging_at_final_admission(oversized):
    from pulsara_agent.conversation_kernel.memory_tools import (
        _rerank_document_projection,
    )
    from pulsara_agent.conversation_kernel.session_content import _fits
    from pulsara_agent.primitives.tool_observation import (
        MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
    )

    chunk = HISTORICAL_IMAGE_MARKER + "\n"
    count = (
        MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES // len(chunk.encode()) + 1
        if oversized
        else 1
    )
    message = {
        "entry_id": "entry:image",
        "sequence": 1,
        "role": "user",
        "label": "用户输入",
        "at": "now",
        "text": "needle " + chunk * count,
        "annotated": False,
        "region": "Q1",
        "body_ranges": [[0, 7]],
        "atomic_ranges": [
            [7 + i * len(chunk), 7 + i * len(chunk) + len(HISTORICAL_IMAGE_MARKER)]
            for i in range(count)
        ],
    }
    document = {
        "messages": [message],
        "body": "needle",
        "positions": [{"start": 0, "end": 6, "message": 0, "display_start": 0}],
    }
    # Body selection is independent of packaging; admission still measures all
    # generated labels against the existing tool and automatic recall limits.
    selected = excerpt_history(document, ([[0, 6]],), budget=2)
    assert selected == (message,)
    candidate = HistoryCandidate(
        "session:test", "entry:image", "", 1, document, selected
    )
    assert (
        _fits({"items": [candidate.projection()], "next_cursor": None}, "call:h")
        is not oversized
    )
    assert (_rerank_document_projection(candidate) is not None) is not oversized


def test_image_marker_is_atomic_when_result_packaging_budget_binds(monkeypatch):
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel import session_content

    marker = image_omission_part(_image()).text
    text = "前\n" + marker + "\n后"
    item = {"role": "user", "entry_id": "entry:image"}
    reader = SimpleNamespace(
        _document=lambda *args: (item, text, (), ((2, 2 + len(marker)),))
    )
    # Simulate the existing final JSON/wrapper limit cutting through a marker.
    monkeypatch.setattr(
        session_content,
        "_fits",
        lambda payload, call: (
            sum(len(x["text"]) for x in payload["items"]) <= len(marker)
        ),
    )
    page = SessionContentQuery._read_page(
        reader,
        None,
        [{"entry_sequence": 1}],
        1,
        0,
        0,
        lambda position, offset: (position, offset),
        1,
        len(text),
        "call:test",
        monotonic() + 30,
        False,
    )
    assert page == {
        "items": [{**item, "text": "前\n", "partial": True}],
        "next_cursor": (1, 2),
    }
    reader._document = lambda *args: (item, marker, (), ((0, len(marker)),))
    monkeypatch.setattr(
        session_content,
        "_fits",
        lambda payload, call: (
            sum(len(x["text"]) for x in payload["items"]) < len(marker)
        ),
    )
    with pytest.raises(SessionQueryError) as exc:
        SessionContentQuery._read_page(
            reader,
            None,
            [{"entry_sequence": 1}],
            1,
            0,
            0,
            lambda position, offset: (position, offset),
            1,
            len(marker),
            "call:test",
            monotonic() + 30,
            False,
        )
    assert exc.value.code == "RESULT_BUDGET_EXCEEDED"
