"""Saved conversation recovery preserves scope, original text and continuation."""

from datetime import datetime, timezone
import json
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantDataBlock,
    AssistantToolCallBlock,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.session_content import (
    SessionContentQuery,
    SessionQueryError,
    _decode,
    _encode,
    _fits,
)
from pulsara_agent.conversation_kernel.session_search import search_session_page
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
from pulsara_agent.conversation_kernel.tool_artifacts import ToolOutputArtifactProcessor
from pulsara_agent.ports.session_content import SessionContentRange
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
    ToolOutputSourceFormatHint,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.test_conversation_fork import (
    repo as repo,
    identity,
    turn,
    rows,
    compact,
    new_session,
)
from tests.test_turn_interruption_history import stop


@pytest.fixture
def context(repo):
    domain = identity("domain").replace(":", "_")

    def new():
        return repo.acquire_host_writer(
            intent="NEW",
            session_id=identity("session"),
            workspace_id=identity("workspace"),
            memory_domain_id=domain,
            writer_owner_id=identity("host"),
            lease_seconds=300,
            deadline_monotonic=monotonic() + 30,
        )

    caller = new()
    owner = SessionContentQuery(
        repo, session_id=caller.guard.session_id, memory_domain_id=domain
    )

    def query(
        name="read_session_content", *, scope=SessionContentRange(0), **arguments
    ):
        return owner.invoke(
            name,
            arguments,
            current_range=scope,
            tool_call_id="call:history",
            deadline_monotonic=monotonic() + 30,
        )

    return caller, new, query


def sequence(repo, entry_id):
    return rows(
        repo,
        "SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE id=%s",
        (entry_id,),
    )[0]["entry_sequence"]


def blob_answer(repo, lease, text, *, extra_blocks=()):
    _, cut, _ = turn(repo, lease.guard, "working", finish=False)
    workspace = rows(
        repo,
        "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
        (lease.guard.session_id,),
    )[0]["workspace_id"]
    body = PostgresCanonicalBlobStore(repo.connection_provider).publish(
        workspace_id=workspace,
        content=text.encode(),
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=monotonic() + 30,
    )
    entry = identity("entry")
    repo.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(AssistantTextBlock(identity("block"), body), *extra_blocks),
        complete_turn=True,
        actor_id="model:test",
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return entry


def saved_calls(repo, lease, blocks):
    _, cut, _ = turn(repo, lease.guard, "working", finish=False)
    entry = identity("entry")
    repo.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=blocks,
        complete_turn=False,
        actor_id="model:test",
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return entry


def call_block(name, arguments):
    return AssistantToolCallBlock(
        identity("block"), identity("call"), name, freeze_json(arguments)
    )


@pytest.mark.parametrize("with_text", [False, True])
def test_mixed_call_search_uses_sources_and_snippet_block_boundaries(
    repo, context, with_text
):
    _, new, query = context
    target = new()
    blocks = [
        call_block(name, {"query": "excluded-sentinel"})
        for name in (
            "search_sessions",
            "search_session_content",
            "read_session_content",
        )
    ]
    if with_text:
        blocks.append(
            AssistantTextBlock(
                identity("block"),
                InlineContent.from_bytes(b"ordinary-text read_session_content"),
            )
        )
    blocks.extend(
        (
            call_block("terminal", {"command": "ordinary-arg"}),
            call_block("read_file", {"path": "second-arg"}),
            AssistantDataBlock(
                identity("block"), InlineContent.from_bytes(b"hidden-data")
            ),
        )
    )
    anchor = saved_calls(repo, target, tuple(blocks))
    args = {"session_id": target.guard.session_id, "include_tools": True}
    for keyword in ("terminal", "command", "ordinary-arg", "second-arg"):
        hits = query("search_session_content", query=keyword, **args)["items"]
        assert len(hits) == 1 and hits[0]["entry_id"] == anchor
        assert keyword in hits[0]["text"]
        assert "excluded-sentinel" not in hits[0]["text"]
        assert "read_session_content" not in hits[0]["text"]
    hits = query("search_session_content", query="ordinary-arg second-arg", **args)[
        "items"
    ]
    assert len(hits) == 1 and hits[0]["entry_id"] == anchor
    for keyword in ("excluded-sentinel", "hidden-data", "Arguments:", "Tool call:"):
        assert query("search_session_content", query=keyword, **args)["items"] == []
    assert query("search_sessions", query="ordinary-arg")["items"] == []
    default = {"session_id": target.guard.session_id, "entry_id": anchor, "limit": 1}
    if with_text:
        assert (
            query(**default)["items"][0]["text"] == "ordinary-text read_session_content"
        )
        assert query(
            "search_session_content", query="ordinary-text read_session_content", **args
        )["items"]
    else:
        with pytest.raises(SessionQueryError) as error:
            query(**default)
        assert error.value.code == "ENTRY_NOT_READABLE"
    read = query(**default, include_tools=True)["items"][0]
    assert read["entry_id"] == anchor and read["role"] == "assistant"
    assert read["text"].startswith(
        'Tool call: search_sessions\nArguments: {"query":"excluded-sentinel"}'
    )
    assert (
        "Tool call: terminal\nArguments:" in read["text"]
        and "hidden-data" not in read["text"]
    )


def test_query_only_calls_do_not_match_or_consume_search_limit(repo, context):
    _, new, query = context
    target = new()
    _, _, old = turn(repo, target.guard, "earlier genuine-needle")
    saved_calls(
        repo,
        target,
        tuple(
            call_block(name, {"query": "genuine-needle"})
            for name in (
                "search_sessions",
                "search_session_content",
                "read_session_content",
            )
        ),
    )
    hit = query(
        "search_session_content",
        session_id=target.guard.session_id,
        include_tools=True,
        query="genuine-needle",
        limit=1,
    )
    assert hit["items"][0]["role"] == "user" and hit["next_cursor"] is None
    assert hit["items"][0]["entry_id"] != old


def test_long_unicode_call_arguments_cursor_only_and_content_v2_hard_cut(repo, context):
    _, new, query = context
    target = new()
    arguments = {"z": ['引号"😀\\\n' * 6000], "a": {"β": False, "null": None}}
    anchor = saved_calls(
        repo,
        target,
        (call_block("terminal", arguments), call_block("read_file", {"path": "last"})),
    )
    expected = (
        "Tool call: terminal\nArguments: "
        + canonical_json_bytes(arguments).decode()
        + '\nTool call: read_file\nArguments: {"path":"last"}'
    )
    page = query(
        session_id=target.guard.session_id,
        entry_id=anchor,
        include_tools=True,
        limit=1,
        max_chars=32000,
    )
    first_cursor = page["next_cursor"]
    assert (
        _decode(first_cursor, "read_session_content")["operation"]
        == "read_session_content.v2"
    )
    chunks = []
    while True:
        assert page["items"][0]["entry_id"] == anchor and _fits(page, "call:history")
        chunks.append(page["items"][0]["text"])
        if not page["next_cursor"]:
            break
        page = query(cursor=page["next_cursor"], limit=1, max_chars=12345)
    assert "".join(chunks) == expected
    old = _decode(first_cursor, "read_session_content")
    old["operation"] = "read_session_content"
    with pytest.raises(SessionQueryError) as error:
        query(cursor=_encode(old))
    assert error.value.code == "CURSOR_INVALID"
    other = new()
    turn(repo, other.guard, "needle", answer="needle")
    page = query(
        "search_session_content",
        session_id=other.guard.session_id,
        query="needle",
        limit=1,
    )
    new_cursor = page["next_cursor"]
    old = _decode(new_cursor, "search_session_content")
    assert old["operation"] == "search_session_content.v2"
    assert query("search_session_content", cursor=new_cursor)["items"]
    old["operation"] = "search_session_content"
    with pytest.raises(SessionQueryError) as error:
        query("search_session_content", cursor=_encode(old))
    assert error.value.code == "CURSOR_INVALID"


@pytest.mark.parametrize(
    "tool_name",
    ["terminal", "search_sessions", "search_session_content", "read_session_content"],
)
@pytest.mark.parametrize(
    "state", ["SUCCESS", "SYSTEM_ERROR", "PERMISSION_DENIED", "CANCELLED"]
)
def test_tool_result_search_uses_canonical_owner_and_read_keeps_all_saved_results(
    repo, stage2_migrated_postgres_database, tool_name, state, monkeypatch
):
    from tests.test_round1_tool_output_artifact import _install_tool_call
    from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES

    caller = new_session(repo)
    workspace = identity("workspace")
    lease, turn_id, request, call, attempt = _install_tool_call(
        repo, workspace, tool_name=tool_name, attempted=state != "PERMISSION_DENIED"
    )
    result_id = identity("entry")
    text = 'result-needle read_session_content {"items":["ordinary output"]}'
    projection = ToolOutputArtifactProcessor(repo.connection_provider).prepare(
        workspace_id=workspace,
        result_entry_id=result_id,
        public_output=text,
        candidate=None,
        artifact_inline_result=False,
        deadline_monotonic=monotonic() + 30,
    )
    accepted = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace,
        result_id=identity("result"),
        result_entry_id=result_id,
        turn_id=turn_id,
        assistant_entry_id=request,
        tool_call_id=call,
        attempt_id=attempt,
        result_state=state,
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="tool:test",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.POLICY
        if attempt is None
        else ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
    )
    repo.accept_tool_result(
        lease.guard, candidate=accepted, deadline_monotonic=monotonic() + 30
    )
    owner = SessionContentQuery(
        repo, session_id=caller.guard.session_id, memory_domain_id="u_local"
    )

    def invoke(name="read_session_content", **args):
        return owner.invoke(
            name,
            args,
            current_range=SessionContentRange(0),
            tool_call_id="call:history",
            deadline_monotonic=monotonic() + 30,
        )

    args = {"session_id": lease.guard.session_id, "include_tools": True}
    hits = invoke("search_session_content", query="result-needle", **args)["items"]
    assert bool(hits) == (tool_name not in SESSION_QUERY_TOOL_NAMES)
    if hits:
        assert hits[0]["entry_id"] == result_id and hits[0]["result_state"] == state
    assert invoke("search_sessions", query="result-needle")["items"] == []
    read = invoke(entry_id=result_id, limit=1, **args)["items"][0]
    assert (
        read["text"] == text
        and read["result_state"] == state
        and read["tool_name"] == tool_name
    )
    with pytest.raises(SessionQueryError) as error:
        invoke(session_id=lease.guard.session_id, entry_id=result_id)
    assert error.value.code == "ENTRY_NOT_READABLE"
    if tool_name in SESSION_QUERY_TOOL_NAMES:
        import pulsara_agent.conversation_kernel.session_content as module

        original = module.decode_saved_text

        def forbidden(row):
            if row["kind"] == "tool":
                raise AssertionError("excluded query result body must not be decoded")
            return original(row)

        monkeypatch.setattr(module, "decode_saved_text", forbidden)
        assert (
            invoke("search_session_content", query="result-needle", **args)["items"]
            == []
        )
        # Canonical edge loss must still fail before a category can be guessed.
        import psycopg

        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            connection.execute(
                "DELETE FROM pulsara_v3.tool_results WHERE session_id=%s AND result_entry_id=%s",
                (lease.guard.session_id, result_id),
            )
        with pytest.raises(RuntimeError, match="canonical edge"):
            invoke("search_session_content", query="result-needle", **args)


def test_full_history_uses_zero_not_genesis_and_explicit_self_cannot_bypass(
    repo, context
):
    caller, _, query = context
    turn(repo, caller.guard, "early decision", answer="older answer")
    _, cut, _ = turn(repo, caller.guard, "current", finish=False)
    facts = (
        CanonicalProviderInputReader(repo.connection_provider)
        .read_frozen_dispatch(cut, deadline_monotonic=monotonic() + 30)
        .compile_snapshot
    )
    assert facts.context_binding_fact.source_through_sequence > 0
    actual = ToolBatchExecutor._session_content_range(facts)
    assert actual == SessionContentRange(0)
    for args in ({}, {"session_id": caller.guard.session_id}):
        assert query(scope=actual, **args) == {
            "items": [],
            "next_cursor": None,
            "note": "没有更早的会话内容。",
        }
        assert (
            query("search_session_content", scope=actual, query="decision", **args)[
                "items"
            ]
            == []
        )
    assert query("search_sessions", scope=actual, query="decision")["items"] == []


def test_snapshot_range_excludes_carried_active_request_and_not_retained_summary(
    repo, context
):
    caller, _, query = context
    _, _, old = turn(repo, caller.guard, "old needle", answer="old original needle")
    active, _, _ = turn(repo, caller.guard, "active needle", finish=False)
    head = rows(
        repo,
        "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s",
        (caller.guard.session_id,),
    )[0]["latest_entry_sequence"]
    compact(
        repo,
        caller.guard,
        active,
        head,
        summary="old original needle already summarized",
    )
    cut = repo.prepare_provider_input_cut(
        caller.guard, turn_id=active, deadline_monotonic=monotonic() + 30
    )
    facts = (
        CanonicalProviderInputReader(repo.connection_provider)
        .read_frozen_dispatch(cut, deadline_monotonic=monotonic() + 30)
        .compile_snapshot
    )
    actual = ToolBatchExecutor._session_content_range(facts)
    assert actual.through_sequence == head and actual.excluded_entry_id is not None
    found = query("search_session_content", scope=actual, query="needle")
    assert {i["entry_id"] for i in found["items"]} == {
        old,
        rows(
            repo,
            "SELECT initial_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at LIMIT 1",
            (caller.guard.session_id,),
        )[0]["initial_entry_id"],
    }
    assert (
        query(scope=actual, entry_id=old, limit=1)["items"][0]["text"]
        == "old original needle"
    )
    with pytest.raises(SessionQueryError, match="readable range"):
        query(scope=actual, entry_id=actual.excluded_entry_id)
    page = query(scope=actual, max_chars=1)
    raw = _decode(page["next_cursor"], "read_session_content")
    raw["excluded"] = None
    with pytest.raises(SessionQueryError) as rejected:
        query(scope=actual, cursor=_encode(raw))
    assert rejected.value.code == "CURSOR_RANGE_CHANGED"


def test_entry_projection_matches_across_blocks_without_data_or_fake_snippet(
    repo, context
):
    _, new, query = context
    target = new()
    body = "first keyword\n  exact spacing  "
    entry = blob_answer(
        repo,
        target,
        body,
        extra_blocks=(
            AssistantDataBlock(
                identity("block"), InlineContent.from_bytes(b"hidden_reasoning")
            ),
            AssistantTextBlock(
                identity("block"), InlineContent.from_bytes(b"second keyword")
            ),
        ),
    )
    result = query(
        "search_session_content",
        session_id=target.guard.session_id,
        query="FIRST second",
    )
    assert len(result["items"]) == 1 and result["items"][0]["entry_id"] == entry
    expected = body + "\nsecond keyword"
    assert result["items"][0]["text"] == expected
    read = query(session_id=target.guard.session_id, entry_id=entry, limit=1)
    assert read["items"][0]["text"] == expected
    ui, _ = search_session_page(
        repo,
        memory_domain_id=rows(
            repo,
            "SELECT memory_domain_id FROM pulsara_v3.sessions WHERE id=%s",
            (target.guard.session_id,),
        )[0]["memory_domain_id"],
        query="first second",
        deadline_monotonic=monotonic() + 30,
    )
    assert len(ui) == 1 and ui[0]["entry_id"] == entry
    assert (
        query(
            "search_session_content",
            session_id=target.guard.session_id,
            query="hidden_reasoning",
        )["items"]
        == []
    )


def test_search_sessions_filters_own_suffix_before_choosing_best_hit(repo, context):
    caller, _, query = context
    _, _, old = turn(repo, caller.guard, "old", answer="decision needle")
    turn(repo, caller.guard, "new suffix needle", answer="new suffix needle")
    scope = SessionContentRange(sequence(repo, old))
    result = query("search_sessions", scope=scope, query="needle")
    assert len(result["items"]) == 1 and result["items"][0]["entry_id"] == old
    assert result["items"][0]["text"] == "decision needle"


def test_anchor_direction_shows_later_revision_without_matching_original_keyword(
    repo, context
):
    _, new, query = context
    target = new()
    _, _, anchor = turn(repo, target.guard, "proposal", answer="Use scheme A")
    _, _, decision = turn(
        repo, target.guard, "reject that", answer="Final decision is B"
    )
    hit = query(
        "search_session_content", session_id=target.guard.session_id, query="scheme"
    )
    assert hit["items"][0]["entry_id"] == anchor
    page = query(session_id=target.guard.session_id, entry_id=anchor)
    assert (
        page["items"][0]["entry_id"] == anchor
        and page["items"][-1]["entry_id"] == decision
    )
    old = query(session_id=target.guard.session_id, entry_id=anchor, direction="older")
    assert [i["text"] for i in old["items"]] == ["Use scheme A", "proposal"]
    assert query(session_id=target.guard.session_id)["items"][0]["entry_id"] == decision
    assert (
        query(session_id=target.guard.session_id, direction="newer")["items"][0]["text"]
        == "proposal"
    )


def test_long_unicode_anchor_stays_one_entry_cursor_only_then_follows_direction(
    repo, context
):
    _, new, query = context
    target = new()
    source = '原文 "quote" 😀\n' * 5000
    anchor = blob_answer(repo, target, source)
    turn(repo, target.guard, "later decision", answer="later final")
    page = query(
        session_id=target.guard.session_id, entry_id=anchor, limit=1, max_chars=32000
    )
    chunks = []
    while page["items"][0]["entry_id"] == anchor:
        assert _fits(page, "call:history")
        assert page["items"][0]["partial"] is True
        chunks.append(page["items"][0]["text"])
        assert page["next_cursor"]
        page = query(cursor=page["next_cursor"], limit=1, max_chars=12345)
    assert "".join(chunks) == source and page["items"][0]["text"] == "later decision"


def test_max_chars_is_whole_page_and_empty_filters_are_ordinary_results(repo, context):
    _, new, query = context
    target = new()
    turn(repo, target.guard, "12345678", answer="abcdefgh")
    page = query(session_id=target.guard.session_id, max_chars=10)
    assert sum(len(i["text"]) for i in page["items"]) == 10
    assert [i["text"] for i in page["items"]] == ["abcdefgh", "12"]
    rest = query(cursor=page["next_cursor"])
    assert rest["items"][0]["text"] == "345678" and rest["items"][0]["partial"]
    assert rest["next_cursor"] is None
    assert query(
        "search_session_content", session_id=target.guard.session_id, query="missing"
    ) == {"items": [], "next_cursor": None}


def test_content_page_freezes_entries_and_interruption_event_cut(repo, context):
    _, new, query = context
    target = new()
    active, _, _ = turn(repo, target.guard, "pending", finish=False)
    entry = rows(
        repo, "SELECT initial_entry_id FROM pulsara_v3.turns WHERE id=%s", (active,)
    )[0]["initial_entry_id"]
    page = query(session_id=target.guard.session_id, entry_id=entry, max_chars=1)
    stop(repo, target.guard, active, "PROVIDER_REQUEST_FAILED", "upstream failure")
    turn(repo, target.guard, "new accepted", answer="new final")
    old = query(cursor=page["next_cursor"])
    assert old["items"][0]["text"] == "ending" and "interruption" not in old["items"][0]
    assert old["next_cursor"] is None
    newer = query(session_id=target.guard.session_id, entry_id=entry, limit=1)
    assert newer["items"][0]["interruption"] == {
        "reason": "PROVIDER_REQUEST_FAILED",
        "detail": "upstream failure",
    }


def test_cursor_tampering_never_expands_current_or_other_scope(repo, context):
    caller, new, query = context
    _, _, old = turn(repo, caller.guard, "before", answer="old answer")
    turn(repo, caller.guard, "current secret", answer="suffix secret")
    scope = SessionContentRange(sequence(repo, old))
    page = query(scope=scope, max_chars=1)
    raw = _decode(page["next_cursor"], "read_session_content")
    raw["range_end"] = raw["cut"]
    with pytest.raises(SessionQueryError, match="range changed"):
        query(scope=scope, cursor=_encode(raw))
    with pytest.raises(SessionQueryError, match="only cursor"):
        query(
            scope=scope, cursor=page["next_cursor"], session_id=caller.guard.session_id
        )
    with pytest.raises(SessionQueryError):
        query("search_session_content", scope=scope, cursor=page["next_cursor"])
    raw = _decode(page["next_cursor"], "read_session_content")
    raw["offset"] = 999999
    with pytest.raises(SessionQueryError, match="outside"):
        query(scope=scope, cursor=_encode(raw))
    target = new()
    _, _, other_entry = turn(repo, target.guard, "other", answer="other")
    with pytest.raises(SessionQueryError, match="readable range"):
        query(scope=scope, entry_id=other_entry)
    turn(repo, target.guard, "second other", answer="more other")
    other_page = query(session_id=target.guard.session_id, max_chars=1)
    other_cursor = _decode(other_page["next_cursor"], "read_session_content")
    other_cursor["session_id"] = caller.guard.session_id
    with pytest.raises(SessionQueryError, match="range changed"):
        query(scope=scope, cursor=_encode(other_cursor))
    foreign = new_session(repo)
    turn(repo, foreign.guard, "foreign")
    with pytest.raises(SessionQueryError, match="domain"):
        query(session_id=foreign.guard.session_id)
    for value in (
        "not-base64",
        _encode({"operation": "read_session_content"}),
        _encode({**raw, "direction": []}),
    ):
        with pytest.raises(SessionQueryError):
            query(scope=scope, cursor=value)


def test_archive_missing_directory_and_fork_are_cold_own_history_only(
    repo, context, tmp_path, stage2_migrated_postgres_database
):
    caller, new, query = context
    target = new()
    _, _, anchor = turn(repo, target.guard, "source", answer="source answer")
    domain = rows(
        repo,
        "SELECT memory_domain_id FROM pulsara_v3.sessions WHERE id=%s",
        (caller.guard.session_id,),
    )[0]["memory_domain_id"]
    workspace_root = tmp_path / "removed-project"
    workspace_root.mkdir()
    import psycopg

    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.workspaces SET workspace_root=%s WHERE id=(SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s)",
            (str(workspace_root), target.guard.session_id),
        )
        connection.commit()
    workspace_root.rmdir()
    child_id = identity("session")
    created = repo.fork_conversation(
        source_session_id=target.guard.session_id,
        anchor_entry_id=anchor,
        child_session_id=child_id,
        memory_domain_id=domain,
        deadline_monotonic=monotonic() + 30,
    )
    assert created.created
    repo.archive_session(
        session_id=target.guard.session_id,
        memory_domain_id=domain,
        closed_writer=target.guard,
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        query(session_id=target.guard.session_id)["items"][0]["text"] == "source answer"
    )
    with pytest.raises(SessionQueryError):
        query(session_id=child_id, entry_id=anchor)
    child = query(session_id=child_id)
    assert (
        child["items"][0]["text"] == "source answer"
        and child["items"][0]["entry_id"] != anchor
    )
    assert (
        repo.delete_session(
            session_id=target.guard.session_id,
            memory_domain_id=domain,
            closed_writer=target.guard,
            deadline_monotonic=monotonic() + 30,
        )
        == "DELETED"
    )
    with pytest.raises(SessionQueryError):
        query(session_id=target.guard.session_id)
    assert query(session_id=child_id) == child
    assert not workspace_root.exists()


@pytest.mark.parametrize(
    "retained, unavailable", [(False, False), (True, False), (False, True)]
)
def test_cross_session_tool_public_metadata_and_full_artifact_are_one_stable_view(
    repo, retained, unavailable, stage2_migrated_postgres_database
):
    from tests.test_round1_tool_output_artifact import (
        _install_tool_call,
        _RecordingPublisher,
    )
    from pulsara_agent.conversation_kernel.tool_artifacts import (
        KnownArtifactPublicationFailure,
        ArtifactContentError,
    )

    caller = new_session(repo)
    workspace = identity("workspace")
    lease, turn_id, request, call, attempt = _install_tool_call(repo, workspace)
    source = (
        "begin\n" + "x" * 50000 + "\nunique-middle-needle\n" + "z" * 50000 + "\nend"
    )
    result_id = identity("entry")
    candidate = ToolOutputArtifactCandidate(
        role="OUTPUT",
        text=source,
        source_coverage=ToolOutputSourceCoverage.RETAINED_SNAPSHOT
        if retained
        else ToolOutputSourceCoverage.COMPLETE,
        source_coverage_reason=ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP
        if retained
        else None,
        source_format_hint=ToolOutputSourceFormatHint.JSON,
    )
    processor = ToolOutputArtifactProcessor(
        repo.connection_provider,
        **(
            {
                "publisher": _RecordingPublisher(
                    failure=KnownArtifactPublicationFailure("storage unavailable")
                )
            }
            if unavailable
            else {}
        ),
    )
    projection = processor.prepare(
        workspace_id=workspace,
        result_entry_id=result_id,
        public_output=json.dumps({"exit_code": 23, "output": source}),
        candidate=candidate,
        artifact_inline_result=False,
        deadline_monotonic=monotonic() + 30,
    )
    accepted = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace,
        result_id=identity("result"),
        result_entry_id=result_id,
        turn_id=turn_id,
        assistant_entry_id=request,
        tool_call_id=call,
        attempt_id=attempt,
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
    repo.accept_tool_result(
        lease.guard, candidate=accepted, deadline_monotonic=monotonic() + 30
    )
    owner = SessionContentQuery(
        repo, session_id=caller.guard.session_id, memory_domain_id="u_local"
    )

    def invoke(name="read_session_content", **args):
        return owner.invoke(
            name,
            args,
            current_range=SessionContentRange(0),
            tool_call_id="call:history",
            deadline_monotonic=monotonic() + 30,
        )

    assert (
        invoke(
            "search_session_content",
            session_id=lease.guard.session_id,
            query="unique-middle-needle",
        )["items"]
        == []
    )
    found = invoke(
        "search_session_content",
        session_id=lease.guard.session_id,
        query="unique-middle-needle",
        include_tools=True,
    )
    if unavailable:
        assert found["items"] == []
        page = invoke(
            session_id=lease.guard.session_id,
            entry_id=result_id,
            include_tools=True,
            limit=1,
        )
        assert (
            page["items"][0]["text"]
            == projection.canonical_preview.canonical_bytes.decode()
        )
        assert page["items"][0]["artifact_disposition"] == "UNAVAILABLE"
        assert page["items"][0]["display_kind"] == "HEAD_TAIL"
        assert page["items"][0]["source_coverage"] == "COMPLETE"
        assert page["next_cursor"] is None
        return
    assert (
        found["items"][0]["entry_id"] == result_id
        and "unique-middle-needle" in found["items"][0]["text"]
    )
    assert (
        invoke(
            "search_session_content",
            session_id=lease.guard.session_id,
            query="Saved tool result:",
            include_tools=True,
        )["items"]
        == []
    )
    page = invoke(
        session_id=lease.guard.session_id,
        entry_id=result_id,
        include_tools=True,
        limit=1,
    )
    parts = []
    while True:
        assert page["items"][0]["entry_id"] == result_id and _fits(page, "call:history")
        parts.append(page["items"][0]["text"])
        if not page["next_cursor"]:
            break
        page = invoke(cursor=page["next_cursor"])
    combined = "".join(parts)
    assert (
        combined
        == "Saved tool result:\n"
        + projection.canonical_preview.canonical_bytes.decode()
        + "\n\nTool output:\n"
        + source
    )
    assert '"exit_code":23' in combined and page["items"][0][
        "artifact_disposition"
    ] == ("INCOMPLETE" if retained else "AVAILABLE")
    if retained:
        assert page["items"][0]["source_coverage"] == "RETAINED_SNAPSHOT"
    # Declared readable output corruption must not silently fall back to preview.
    import psycopg

    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
            (b"x" * len(source.encode()), projection.artifact_blob.blob_id),
        )
    with pytest.raises(ArtifactContentError):
        invoke(
            session_id=lease.guard.session_id, entry_id=result_id, include_tools=True
        )


def test_native_schemas_support_cursor_only_and_tools_are_read_only_root_only():
    from jsonschema import validate
    from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
    from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES
    from pulsara_agent.conversation_kernel.tool_runtime import _json_schema_value

    for name in SESSION_QUERY_TOOL_NAMES:
        entry = builtin_tool_catalog_entry(name)
        validate(
            {"cursor": "returned"}, _json_schema_value(entry.descriptor.input_schema)
        )
        assert (
            entry.descriptor.is_read_only
            and entry.recovery_contract.severity == "read_only"
        )
        assert len(entry.availability_requirement.allowed_invocation_owners) == 1


def test_direct_query_uses_physical_owner_cancel_settlement_and_root_surface(tmp_path):
    import asyncio
    from dataclasses import replace
    from threading import Event
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.conversation_kernel.tool_policy import (
        DefaultToolDispatchAuthorizationPolicy,
    )
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES
    from tests.support.round3 import (
        direct_tool_invocation_context,
        prepare_test_direct_tool_surface,
    )
    from pulsara_agent.storage.migrations.errors import (
        PostgresSchemaError,
        PostgresSchemaFailureCode,
    )

    started, release = Event(), Event()

    class Query:
        failure = None

        def invoke(
            self,
            operation,
            arguments,
            *,
            current_range,
            tool_call_id,
            deadline_monotonic,
        ):
            assert current_range == SessionContentRange(0)
            if self.failure:
                raise self.failure
            started.set()
            assert release.wait(5)
            return {"items": [], "next_cursor": None}

    query = Query()

    async def run():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:query",
            session_id="session:query",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            session_content_query=query,
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id="session:query",
            tool_name="read_session_content",
            tool_call_id="call:query",
            attempt_id="attempt:query",
            turn_id="turn:query",
            assistant_entry_id="entry:query",
        )
        context = replace(context, session_content_range=SessionContentRange(0))

        async def invoke():
            return await port.invoke(
                tool_name="read_session_content",
                arguments={},
                tool_call_id="call:query",
                attempt_id="attempt:query",
                turn_id="turn:query",
                assistant_entry_id="entry:query",
                invocation_context=context,
            )

        try:
            child = prepare_test_direct_tool_surface(
                port,
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id="task:query",
            )
            assert SESSION_QUERY_TOOL_NAMES.isdisjoint(
                s.name for s in child.model_surface.tool_specs
            )
            operation = asyncio.create_task(invoke())
            assert await asyncio.to_thread(started.wait, 5)
            assert (
                port._physical_io._active
            )  # Actual admitted owner, also drained by aclose.
            operation.cancel()
            await asyncio.sleep(0)
            release.set()
            result = await operation
            assert result.state == "SUCCESS" and result.caller_cancelled_while_running
            assert (
                result.physical_timing == "LATE_AFTER_WATCHDOG"
                and result.artifact_inline_result
            )
            assert json.loads(result.content) == {"items": [], "next_cursor": None}
            for error, expected in [
                (
                    PostgresSchemaError(
                        PostgresSchemaFailureCode.CONNECTION_FAILED, "offline"
                    ),
                    "QUERY_UNAVAILABLE",
                ),
                (
                    PostgresSchemaError(
                        PostgresSchemaFailureCode.DEADLINE_EXCEEDED, "expired"
                    ),
                    "QUERY_TIMEOUT",
                ),
                (RuntimeError("corrupt text"), "CONTENT_CORRUPT"),
            ]:
                query.failure = error
                result = await invoke()
                assert (
                    result.state == "APPLICATION_ERROR"
                    and json.loads(result.content)["code"] == expected
                )
        finally:
            release.set()
            borrow.close()
            await port.aclose()

    asyncio.run(run())
