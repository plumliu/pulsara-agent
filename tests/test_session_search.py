"""Session search reads accepted visible content, with no history truncation."""

import asyncio
from datetime import datetime, timezone
from time import monotonic
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
)
from pulsara_agent.conversation_kernel.session_search import search_session_page
from pulsara_agent.primitives.context import freeze_json
from tests.test_conversation_fork import repo as repo, identity, turn, rows


@pytest.fixture
def search_context(repo):
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

    def search(query="", **kwargs):
        return search_session_page(
            repo,
            memory_domain_id=domain,
            query=query,
            deadline_monotonic=monotonic() + 30,
            **kwargs,
        )

    return domain, new, search


def test_search_titles_users_final_commentary_and_long_blob_without_duplicates(
    repo, search_context
):
    domain, new, search = search_context
    lease = new()
    sid = lease.guard.session_id
    turn(repo, lease.guard, "用户中文 ABC %_.*", answer="final answer 唯一最终词")
    turn(repo, lease.guard, "another question", answer="old discussion 搜索词")
    _, cut, _ = turn(repo, lease.guard, "working", finish=False)
    blob = PostgresCanonicalBlobStore(repo.connection_provider).publish(
        workspace_id=rows(
            repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (sid,)
        )[0]["workspace_id"],
        content=("长" * 25000 + " 搜索词 blob尾端").encode(),
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=monotonic() + 30,
    )
    repo.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=identity("entry"),
        parent_content=InlineContent.from_bytes(b"excluded-manifest"),
        blocks=(
            AssistantTextBlock(identity("block"), blob),
            AssistantDataBlock(
                identity("block"), InlineContent.from_bytes(b"excluded-thinking-data")
            ),
            AssistantToolCallBlock(
                identity("block"),
                identity("call"),
                "terminal",
                freeze_json({"command": "excluded-tool-argument"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    before = rows(repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (sid,))
    for query, kind in [
        ("用户中文 abc %_.*", "user"),
        ("唯一最终词", "assistant"),
        ("blob尾端", "assistant"),
    ]:
        found, cursor = search(query)
        assert (
            len(found) == 1 and found[0]["id"] == sid and found[0]["match_kind"] == kind
        )
        assert cursor is None
    found, _ = search("搜索词")
    assert len(found) == 1 and "blob尾端" in found[0]["snippet"]
    assert len(found[0]["snippet"]) <= 242
    for excluded in (
        "excluded-manifest",
        "excluded-thinking-data",
        "excluded-tool-argument",
        "用户中文 唯一最终词",
    ):
        assert search(excluded) == ([], None)
    assert rows(repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (sid,)) == before
    fallback = "会话 " + sid.removeprefix("session:")[:8]
    assert search(fallback)[0][0]["match_kind"] == "title"
    repo.rename_session(
        session_id=sid,
        memory_domain_id=domain,
        title="搜索词",
        deadline_monotonic=monotonic() + 30,
    )
    assert search("搜索词")[0][0]["rank"] == 0


def test_search_pages_all_matches_across_workspaces_and_lifecycle(repo, search_context):
    domain, new, search = search_context
    leases = [new() for _ in range(5)]
    for lease in leases:
        turn(repo, lease.guard, "shared needle")
    archived = leases[0]
    repo.archive_session(
        session_id=archived.guard.session_id,
        memory_domain_id=domain,
        closed_writer=archived.guard,
        deadline_monotonic=monotonic() + 30,
    )
    all_ids = []
    cursor = None
    while True:
        page, cursor = search("needle", limit=2, cursor=cursor)
        all_ids.extend(row["id"] for row in page)
        if not cursor:
            break
    assert len(all_ids) == 5 and set(all_ids) == {
        lease.guard.session_id for lease in leases
    }
    assert len(search("needle", lifecycle="OPEN")[0]) == 4
    assert [r["id"] for r in search("needle", lifecycle="ARCHIVED")[0]] == [
        archived.guard.session_id
    ]
    first, cursor = search("needle", limit=2)
    assert cursor
    with pytest.raises(ValueError, match="cursor"):
        search("different", cursor=cursor)
    with pytest.raises(ValueError, match="cursor"):
        search("needle", lifecycle="OPEN", cursor=cursor)
    assert search_session_page(
        repo,
        memory_domain_id=identity("other-domain"),
        query="needle",
        cursor=cursor,
        deadline_monotonic=monotonic() + 30,
    ) == ([], None)
    assert all(
        row["match_kind"] == "recent" and row["snippet"] == "" for row in search()[0]
    )
    assert (
        repo.delete_session(
            session_id=leases[1].guard.session_id,
            memory_domain_id=domain,
            closed_writer=leases[1].guard,
            deadline_monotonic=monotonic() + 30,
        )
        == "DELETED"
    )
    assert len(search("needle")[0]) == 4


def test_fork_search_uses_child_history_ownership(repo, search_context):
    domain, new, search = search_context
    source = new()
    _, _, anchor = turn(
        repo, source.guard, "forked-user-text", answer="forked-assistant-text"
    )
    child = identity("session")
    repo.fork_conversation(
        source_session_id=source.guard.session_id,
        anchor_entry_id=anchor,
        child_session_id=child,
        memory_domain_id=domain,
        deadline_monotonic=monotonic() + 30,
    )
    for query in ("forked-user-text", "forked-assistant-text"):
        assert {r["id"] for r in search(query)[0]} == {source.guard.session_id, child}


def test_http_search_rejects_invalid_fields_origins_and_reports_timeout(tmp_path):
    from typing import cast
    from aiohttp import ClientSession
    from pulsara_agent.web_app.http_server import LocalHttpServer
    from pulsara_agent.web_app.session_controller import LocalSessionController
    from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
    from tests.test_local_web_http_surface import (
        _Sessions,
        _Bridge,
        _model_server_dependencies,
    )

    async def run():
        sessions = _Sessions()
        sessions.search_sessions = AsyncMock(
            return_value={"items": [], "next_cursor": None}
        )
        (tmp_path / "index.html").write_text("Pulsara")
        server = LocalHttpServer(
            sessions=cast(LocalSessionController, sessions),
            bridge=cast(LocalBrowserBridge, _Bridge()),
            static_root=tmp_path,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            async with ClientSession() as client:
                url = f"{server.origin}/api/sessions/search"
                async with client.post(
                    url, json={"query": "x"}, headers={"Origin": "https://other.test"}
                ) as response:
                    assert response.status == 403
                for body in (
                    {},
                    {"query": 1},
                    {"query": "x", "unexpected": True},
                    {"query": "x", "limit": True},
                    {"query": "x", "limit": 51},
                    {"query": "x", "lifecycle": "other"},
                    {"query": "x", "cursor": 3},
                ):
                    async with client.post(url, json=body) as response:
                        assert response.status == 400
                sessions.search_sessions.assert_not_awaited()
                async with client.post(
                    url, json={"query": "中文", "lifecycle": "ARCHIVED", "limit": 2}
                ) as response:
                    assert response.status == 200
                    assert await response.json() == {"items": [], "next_cursor": None}
                sessions.search_sessions.assert_awaited_once_with(
                    query="中文", lifecycle="ARCHIVED", limit=2
                )
                sessions.search_sessions.side_effect = TimeoutError("deadline")
                async with client.post(url, json={"query": "x"}) as response:
                    assert response.status == 503
                    assert "SESSION_SEARCH_TIMEOUT" in await response.text()
        finally:
            await server.aclose()

    asyncio.run(run())


def test_subagent_internal_text_and_tool_result_are_not_search_documents(
    repo, search_context
):
    from tests.test_round5_long_horizon_postgres import (
        _runner,
        _text_stream,
        _tool_stream,
        _round10_subagent_runtime,
    )
    from tests.support.round3 import ScriptedKernelModel
    from tests.support.subagents import (
        accept_active_subagent_fixture,
        run_admitted_subagent_fixture,
    )
    from tests.support.model_config import (
        frozen_test_prompt,
        test_model_binding as model_binding,
        test_model_runtime as model_runtime,
    )

    _, new, search = search_context
    lease = new()
    repo.update_session_model_call_binding(
        lease.guard,
        binding=model_binding(model_runtime()),
        deadline_monotonic=monotonic() + 30,
    )
    # The real Runner settles a tool result, then commits visible assistant text.
    model = ScriptedKernelModel(
        [_tool_stream(0), _text_stream("root-visible-final", block_id="root-answer")]
    )
    result = asyncio.run(
        _runner(repo, lease, model).run_turn(
            frozen_test_prompt("root-visible-question")
        )
    )
    assert result.tool_call_count == 1
    tool_rows = rows(
        repo,
        "SELECT inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='TOOL_RESULT'",
        (lease.guard.session_id,),
    )
    assert tool_rows and tool_rows[0]["inline_content"]
    tool_text = bytes(tool_rows[0]["inline_content"]).decode()
    assert search(tool_text) == ([], None)
    assert len(search("root-visible-final")[0]) == 1
    parent_turn, _, _ = turn(repo, lease.guard, "parent-question", finish=False)
    child = accept_active_subagent_fixture(
        repo, lease, parent_turn_id=parent_turn, objective="excluded-child-objective"
    )
    model = ScriptedKernelModel(
        [_text_stream("excluded-child-answer", block_id="child-answer")]
    )
    runner = _runner(
        repo,
        lease,
        model,
        subagent_runtime=_round10_subagent_runtime(repo, lease, child),
    )
    result = asyncio.run(run_admitted_subagent_fixture(runner, child))
    assert result.final_text == "excluded-child-answer"
    assert search("excluded-child-answer") == ([], None)
    assert search("excluded-child-objective") == ([], None)


def test_annotation_comment_is_searchable_without_duplicating_quoted_sources(
    repo, search_context
):
    from pulsara_agent.llm.input import (
        FrozenPromptContent,
        LLMTextPart,
        PromptAnnotationPart,
        PromptAnnotationSource,
    )
    from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
    from tests.support.model_config import (
        start_test_root_turn,
        test_model_binding as binding,
        test_model_runtime as runtime,
    )

    _, new, search = search_context
    lease = new()
    _, _, source = turn(repo, lease.guard, "original", answer="inert-quoted-source")
    start_test_root_turn(
        repo,
        lease.guard,
        command_id=identity("command"),
        turn_id=identity("turn"),
        entry_id=identity("entry"),
        context_binding_revision_id=identity("revision"),
        permission_snapshot_id=identity("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=binding(runtime()),
        content=FrozenPromptContent(
            (
                LLMTextPart("user-body"),
                PromptAnnotationPart(
                    "inert-quoted-source",
                    PromptAnnotationSource(source, 0, len("inert-quoted-source")),
                    "active-comment",
                ),
            )
        ),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert len(search("active-comment")[0]) == 1
    assert search("inert-quoted-source")[0][0]["match_kind"] == "assistant"


def test_recent_five_are_selected_after_lifecycle_filtering(repo, search_context):
    domain, new, search = search_context
    leases = [new() for _ in range(14)]
    for lease in leases[::2]:
        repo.archive_session(session_id=lease.guard.session_id, memory_domain_id=domain,
                             closed_writer=lease.guard, deadline_monotonic=monotonic()+30)
    ordered = rows(repo, """SELECT id, lifecycle FROM pulsara_v3.sessions
        WHERE memory_domain_id=%s
        ORDER BY CASE WHEN lifecycle='ARCHIVED' THEN updated_at ELSE created_at END DESC, id""", (domain,))
    for lifecycle in ('ALL', 'OPEN', 'ARCHIVED'):
        expected = [row['id'] for row in ordered if lifecycle == 'ALL' or row['lifecycle'] == lifecycle][:5]
        found, cursor = search('', lifecycle=lifecycle, limit=5)
        assert [row['id'] for row in found] == expected
        assert len(found) == 5
        assert cursor is not None  # Only the UI recent preview stops at five; the corpus is uncapped.
