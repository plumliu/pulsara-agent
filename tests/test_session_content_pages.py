"""History pages obey the actual call budget without changing source ranges."""

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.session_content import (
    SessionContentQuery,
    _decode,
    _encode,
)
from pulsara_agent.conversation_kernel.session_text import prompt_history_text
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.history_projection import canonical_history_message
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMTextPart, PromptAnnotationPart
from pulsara_agent.ports.session_content import SessionContentRange, session_entry_target
from pulsara_agent.ports.tool_output import ToolOutputContentError
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBoundary,
    ToolResultPageBudget,
)


def page_owner(text, *, bounds=()):
    owner = object.__new__(SessionContentQuery)
    target = session_entry_target(random_id("session"), random_id("entry"))
    owner._document = lambda *_args: (
        {"target": target, "role": "assistant", "at": "2026-11-03T00:00:00Z"},
        text,
        ((0, text),),
        bounds,
    )
    return owner, target


def read_page(owner, budget, *, offset=0, maximum=32_000):
    return owner._read_page(
        None,
        [{"entry_sequence": 3}],
        3,
        offset,
        5,
        lambda sequence, start: _encode({"operation": "read_session_content.v4", "sequence": sequence, "offset": start}),
        1,
        maximum,
        "call:history",
        monotonic() + 30,
        False,
        "unused-workspace",
        budget,
    )


def test_history_long_entry_uses_provider_allowance_and_complete_next():
    text = '引号"\\😀\n' * 150
    owner, target = page_owner(text)
    budget = ToolResultPageBudget(
        tool_call_id="call:history",
        accepts_provider_body=lambda body, _ids: len(body.encode("utf-8")) <= 700,
    )
    offset, pieces, pages = 0, [], 0
    while True:
        page = read_page(owner, budget, offset=offset)
        assert "search_session_content" in page["guidance"]
        assert budget.fits(json.dumps(page, ensure_ascii=False, separators=(",", ":")))
        assert page["items"][0]["target"] == target
        pieces.append(page["items"][0]["text"])
        pages += 1
        if page["next"] is None:
            break
        assert page["next"]["tool"] == "read_session_content"
        arguments = page["next"]["arguments"]
        assert set(arguments) == {"cursor", "limit", "max_chars"}
        position = _decode(arguments["cursor"], "read_session_content")
        assert position["sequence"] == 3 and position["offset"] > offset
        offset = position["offset"]
    assert pages > 1 and "".join(pieces) == text


def test_history_page_cannot_silently_skip_an_atomic_image_placeholder():
    placeholder = "[historical image " + "x" * 100 + "]"
    owner, _ = page_owner(placeholder, bounds=((0, len(placeholder)),))
    budget = ToolResultPageBudget(tool_call_id="call:history")
    with pytest.raises(ToolResultPageBoundary, match="complete image placeholder"):
        read_page(owner, budget, maximum=10)
    assert read_page(owner, budget)["items"][0]["text"] == placeholder


def test_history_quote_is_readable_but_only_comment_is_a_search_segment():
    quote, comment = "source-only needle", "the user's actual comment"
    text, segments, bounds = prompt_history_text(
        (LLMTextPart("before"), PromptAnnotationPart(quote, None, comment)),
        image_references=False,
    )
    assert quote in text and comment in text and not bounds
    assert all(quote not in value for _, value in segments)
    assert segments[-1] == (text.index(comment), comment)


def test_tool_history_reads_only_owned_body_and_keeps_facts_and_source_gap():
    source = "complete retained log Ω"
    content = InlineContent.from_bytes(source.encode())
    fact = {
        "tool_name": "exec_process", "source_kind": "MODEL_TOOL_CALL",
        "result_state": "SUCCESS", "result_facts": {"exit_code": 23},
        "output_unavailability_reason": None,
        "output_source_coverage": "RETAINED_SNAPSHOT",
        "output_source_coverage_reason": "TERMINAL_RETENTION_GAP",
        "output_body_inline_content": content.canonical_bytes,
        "output_body_blob_id": None,
        "output_body_digest": content.digest, "output_body_size": content.size,
        "output_body_media_type": content.media_type, "output_body_codec": content.codec,
    }
    connection = SimpleNamespace(execute=lambda *_args: SimpleNamespace(fetchone=lambda: fact))
    entry = {
        "entry_kind": "TOOL_RESULT", "session_id": random_id("session"),
        "id": random_id("entry"), "origin_entry_id": random_id("entry"),
        "entry_sequence": 3, "workspace_id": random_id("workspace"),
        "scheduled_input": None, "accepted_at": datetime.now(timezone.utc),
        "content_media_type": "text/plain", "inline_content": b"native preview must not be read",
    }
    owner = object.__new__(SessionContentQuery)
    owner.reader = SimpleNamespace(_notices=lambda *_args: [])
    item, text, _, _ = owner._document(
        connection, entry, 4, monotonic() + 30, True, entry["workspace_id"]
    )
    assert text == source
    assert item["result_facts"] == {"exit_code": 23}
    assert item["source_coverage"] == "RETAINED_SNAPSHOT"
    indexed = canonical_history_message(connection, entry, include_tools=True)
    assert indexed["text"].endswith(source)
    assert '"exit_code":23' in indexed["text"]
    assert "native preview" not in indexed["text"]
    assert indexed["source_coverage"] == "RETAINED_SNAPSHOT"
    fact["output_body_inline_content"] = b"x" * content.size
    with pytest.raises(ToolOutputContentError, match="integrity"):
        owner._document(connection, entry, 4, monotonic() + 30, True, entry["workspace_id"])


@pytest.mark.parametrize("empty", [False, True])
def test_session_search_guidance_is_quoted_with_empty_and_budget_limited_pages(monkeypatch, empty):
    from pulsara_agent.conversation_kernel import session_content

    owner = object.__new__(SessionContentQuery)
    owner.repository = object()
    owner.memory_domain_id = "u_local"
    owner.session_id = random_id("session")
    rows = [] if empty else [
        {"id": random_id("session"), "title": "x" * 500,
         "workspace_label": "project", "updated_at": datetime.now(timezone.utc), "rank": 0}
        for _ in range(2)
    ]
    monkeypatch.setattr(session_content, "search_session_page", lambda *_args, **_kwargs: (rows, None))
    quotes = []
    def accepts(body, _ids):
        value = json.loads(body)
        assert "search_session_content" in value["guidance"]
        assert "read_session_content" in value["guidance"]
        quotes.append(value)
        return len(body.encode("utf-8")) <= 1500
    budget = ToolResultPageBudget(tool_call_id="call:history", accepts_provider_body=accepts)
    page = owner._sessions(None, {}, None, SessionContentRange(0), 2,
                           "call:history", monotonic() + 30, budget)
    assert quotes and budget.fits(json.dumps(page, ensure_ascii=False, separators=(",", ":")))
    assert len(page["items"]) == (0 if empty else 1)
    if empty:
        assert page["next"] is None
    else:
        assert page["items"][0]["target"]["session_id"] == rows[0]["id"]
        assert page["next"]["tool"] == "search_sessions"
        assert set(page["next"]["arguments"]) == {"cursor", "limit"}


@pytest.mark.parametrize("empty", [False, True])
def test_content_search_guidance_preserves_read_calls_and_page_budget(monkeypatch, empty):
    from pulsara_agent.conversation_kernel import history_search

    owner = object.__new__(SessionContentQuery)
    owner.session_id = random_id("session")
    owner.memory_domain_id = "u_local"
    owner._history_consumed_pages = {}
    owner._authorized = lambda *_args: None
    target_session = random_id("session")
    candidates = [] if empty else [
        SimpleNamespace(
            session_id=target_session, q1_entry_id=random_id("entry"),
            title="topic", score=1.0,
            messages=[{"entry_id": random_id("entry"), "role": "assistant",
                       "label": "Answer", "at": "2026-11-04T00:00:00Z", "text": "x" * 700}],
        )
        for _ in range(2)
    ]
    rows = [{"q1_entry_id": candidate.q1_entry_id, "candidate": candidate} for candidate in candidates]
    monkeypatch.setattr(history_search, "search_history", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(history_search, "refetch_history", lambda _connection, row, **_kwargs: row["candidate"])
    quotes = []
    def accepts(body, _ids):
        value = json.loads(body)
        assert "read_session_content" in value["guidance"]
        quotes.append(value)
        return len(body.encode("utf-8")) <= 2000
    budget = ToolResultPageBudget(tool_call_id="call:history", accepts_provider_body=accepts)
    page = owner._search_content_bm25(
        None, {"query": "topic", "include_tools": True,
               "target": {"kind": "SESSION", "session_id": target_session}},
        None, SessionContentRange(0), 2, "call:history", monotonic() + 30, budget,
    )
    assert quotes and budget.fits(json.dumps(page, ensure_ascii=False, separators=(",", ":")))
    assert len(page["items"]) == (0 if empty else 1)
    if empty:
        assert page["next"] is None
    else:
        message = page["items"][0]["messages"][0]
        assert message["read"] == {"tool": "read_session_content", "arguments": {
            "target": message["target"], "limit": 1, "include_tools": True}}
        assert page["next"]["tool"] == "search_session_content"
        assert set(page["next"]["arguments"]) == {"cursor", "limit"}


def test_empty_current_history_guidance_uses_the_final_result_allowance():
    owner = object.__new__(SessionContentQuery)
    owner.session_id = random_id("session")
    owner._authorized = lambda *_args: {
        "latest_entry_sequence": 1, "latest_event_sequence": 0,
        "workspace_id": random_id("workspace"),
    }
    owner.reader = SimpleNamespace(
        _connection=lambda *_args: nullcontext(None),
        validate_history_cut=lambda *_args: None,
    )
    quotes = []
    def accepts(body, _ids):
        page = json.loads(body)
        assert "search_session_content" in page["guidance"]
        quotes.append(page)
        return True
    def invoke(budget):
        return owner.invoke(
            "read_session_content", {}, current_range=SessionContentRange(0),
            tool_call_id="call:history", page_budget=budget,
            deadline_monotonic=monotonic() + 30,
        )
    page = invoke(ToolResultPageBudget(tool_call_id="call:history", accepts_provider_body=accepts))
    assert quotes == [page]
    assert page == {"items": [], "next": None, "note": "没有更早的会话内容。",
                    "guidance": page["guidance"]}
    without_guidance = {key: value for key, value in page.items() if key != "guidance"}
    allowance = len(json.dumps(without_guidance, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    with pytest.raises(ToolResultPageBoundary):
        invoke(ToolResultPageBudget(
            tool_call_id="call:history",
            accepts_provider_body=lambda body, _ids: len(body.encode("utf-8")) <= allowance,
        ))
