"""Complete history business scope, direction and frozen source qualification."""

from datetime import datetime, timezone
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantDataBlock,
)
from pulsara_agent.conversation_kernel.session_content import (
    SessionContentQuery,
    SessionQueryError,
)
from pulsara_agent.ports.session_content import SessionContentRange
from tests.test_conversation_fork import (
    repo as repo,
    new_session,
    turn,
    rows,
    identity,
    final,
)
from tests.support.history_projection import maintain_history

pytestmark = pytest.mark.postgres


def query(repo, caller, operation, *, scope=None, **args):
    from pulsara_agent.ports.session_content import session_query_result_schema
    from tests.support.tool_result_schemas import validate_business_result

    value = SessionContentQuery(
        repo, session_id=caller.guard.session_id, memory_domain_id="u_local"
    ).invoke(
        operation,
        args,
        current_range=scope if scope is not None else SessionContentRange(0),
        deadline_monotonic=monotonic() + 30,
    )
    validate_business_result(session_query_result_schema(operation), value)
    return value


def test_complete_history_read_signed_direction_labels_and_domain(repo):
    caller, source = new_session(repo), new_session(repo)
    owner, cut, _ = turn(repo, source.guard, "first question", finish=False)
    commentary = identity("entry")
    repo.commit_assistant_message(
        source.guard,
        cut=cut,
        entry_id=commentary,
        blocks=(
            AssistantTextBlock(
                identity("block"), InlineContent.from_bytes(b"progress\r\n")
            ),
            AssistantDataBlock(
                identity("block"), InlineContent.from_bytes(b"private thought")
            ),
        ),
        complete_turn=False,
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
    answer = final(repo, source.guard, owner, "final body")
    _, _, second = turn(repo, source.guard, "second question", answer="second answer")
    records = query(
        repo,
        caller,
        "read_session_content",
        target={"kind": "SESSION", "session_id": source.guard.session_id},
        limit=-3,
    ).content
    assert [p.value["role"] for p in records] == [
        "assistant final answer",
        "user message",
        "assistant final answer",
    ]
    assert records[0].value["target"] == {"kind": "SESSION_ENTRY", "entry_id": second}
    forward = query(
        repo,
        caller,
        "read_session_content",
        target={"kind": "SESSION_ENTRY", "entry_id": commentary},
        limit=2,
    ).content
    assert [p.value["text"] for p in forward] == ["progress\r\n", "final body"]
    assert [p.value["role"] for p in forward] == [
        "assistant commentary",
        "assistant final answer",
    ]
    assert all(set(p.value) == {"target", "role", "at", "text"} for p in forward)
    assert "private thought" not in repr(forward)
    for args in (
        {
            "target": {"kind": "SESSION", "session_id": source.guard.session_id},
            "limit": 1,
        },
        {"target": {"kind": "SESSION", "session_id": source.guard.session_id}},
        {"target": {"kind": "SESSION_ENTRY", "entry_id": answer}, "limit": 0},
        {
            "target": {
                "kind": "SESSION_ENTRY",
                "entry_id": answer,
                "session_id": source.guard.session_id,
            },
            "limit": 1,
        },
    ):
        with pytest.raises(SessionQueryError):
            query(repo, caller, "read_session_content", **args)


def test_session_search_complete_entry_precedes_title_without_entry_payload(
    repo, tmp_path, stage2_migrated_postgres_database
):
    caller, source, title_only = new_session(repo), new_session(repo), new_session(repo)
    turn(repo, source.guard, "entryneedle body", answer="answer")
    turn(repo, title_only.guard, "different body", answer="answer")
    import psycopg

    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        for index, lease in enumerate((source, title_only)):
            actual = tmp_path / f"workspace-{index}"
            actual.mkdir()
            connection.execute(
                "UPDATE pulsara_v3.workspaces SET workspace_root=%s WHERE id=(SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s)",
                (str(actual), lease.guard.session_id),
            )
    for lease in (source, title_only):
        assert repo.adopt_generated_session_title(
            session_id=lease.guard.session_id,
            memory_domain_id="u_local",
            title="entryneedle title",
            deadline_monotonic=monotonic() + 30,
        )
    records = query(repo, caller, "search_sessions", query="entryneedle").content
    found = {p.value["target"]["session_id"]: p.value for p in records}
    assert found[source.guard.session_id]["matched_in"] == "ENTRY"
    assert found[title_only.guard.session_id]["matched_in"] == "TITLE"
    assert all(
        set(p.value)
        == {"target", "title", "workspace_path", "updated_at", "matched_in"}
        for p in records
    )
    assert all(p.value["workspace_path"].startswith("/") for p in records)
    with pytest.raises(SessionQueryError):
        query(repo, caller, "search_sessions", limit=1)


def test_qa_search_uses_complete_candidates_and_four_label_text(repo):
    caller, source = new_session(repo), new_session(repo)
    turn(repo, source.guard, "alpha needle?", answer="answer needle")
    turn(repo, source.guard, "follow up", answer="next answer")
    maintain_history(repo)
    value = query(
        repo,
        caller,
        "search_session_content",
        target={"kind": "SESSION", "session_id": source.guard.session_id},
        query="needle",
    )
    assert value.content and all(p.type == "object" for p in value.content)
    for part in value.content:
        assert set(part.value) == {"target", "text"}
        assert set(part.value["target"]) == {"kind", "entry_id"}
        assert "user message (" in part.value["text"]
        assert "assistant final answer (" in part.value["text"]
        assert "needle" in part.value["text"]


def test_session_query_tools_are_read_only_root_only():
    from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
    from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES

    for name in SESSION_QUERY_TOOL_NAMES:
        entry = builtin_tool_catalog_entry(name)
        assert (
            entry.descriptor.is_read_only
            and entry.recovery_contract.severity == "read_only"
        )
        assert len(entry.availability_requirement.allowed_invocation_owners) == 1


def test_current_context_cannot_bypass_earlier_content_scope(repo):
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
    from pulsara_agent.ports.session_content import session_content_range

    caller = new_session(repo)
    turn(repo, caller.guard, "early decision", answer="older answer")
    _, cut, _ = turn(repo, caller.guard, "current", finish=False)
    facts = (
        CanonicalProviderInputReader(repo.connection_provider)
        .read_frozen_dispatch(cut, deadline_monotonic=monotonic() + 30)
        .compile_snapshot
    )
    scope = session_content_range(facts)
    assert scope == SessionContentRange(0)
    value = query(
        repo,
        caller,
        "read_session_content",
        scope=scope,
        target={"kind": "SESSION", "session_id": caller.guard.session_id},
        limit=-10,
    )
    assert len(value.content) == 1 and value.content[0].type == "text"
    assert (
        query(repo, caller, "search_sessions", scope=scope, query="decision")
        .content[0]
        .type
        == "text"
    )


def test_compacted_read_excludes_active_request_and_reads_original_not_summary(repo):
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
    from pulsara_agent.ports.session_content import session_content_range
    from tests.test_conversation_fork import compact

    caller = new_session(repo)
    _, _, answer = turn(repo, caller.guard, "old needle", answer="old original needle")
    active, _, _ = turn(repo, caller.guard, "active needle", finish=False)
    head = rows(
        repo,
        "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s",
        (caller.guard.session_id,),
    )[0]["latest_entry_sequence"]
    compact(repo, caller.guard, active, head, summary="replacement summary text")
    cut = repo.prepare_provider_input_cut(
        caller.guard, turn_id=active, deadline_monotonic=monotonic() + 30
    )
    facts = (
        CanonicalProviderInputReader(repo.connection_provider)
        .read_frozen_dispatch(cut, deadline_monotonic=monotonic() + 30)
        .compile_snapshot
    )
    scope = session_content_range(facts)
    assert scope.through_sequence == head and scope.excluded_entry_id is not None
    value = query(
        repo,
        caller,
        "read_session_content",
        scope=scope,
        target={"kind": "SESSION_ENTRY", "entry_id": answer},
        limit=1,
    )
    assert value.content[0].value["text"] == "old original needle"
    with pytest.raises(SessionQueryError):
        query(
            repo,
            caller,
            "read_session_content",
            scope=scope,
            target={"kind": "SESSION_ENTRY", "entry_id": scope.excluded_entry_id},
            limit=1,
        )


def test_cold_archived_history_survives_missing_directory_and_parent_deletion(
    repo, tmp_path, stage2_migrated_postgres_database
):
    import psycopg
    from tests.test_conversation_fork import fork

    caller, source = new_session(repo), new_session(repo)
    _, _, anchor = turn(repo, source.guard, "source", answer="source answer")
    missing = tmp_path / "missing-workspace"
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.workspaces SET workspace_root=%s WHERE id=(SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s)",
            (str(missing), source.guard.session_id),
        )
    created = fork(repo, source.guard.session_id, anchor)
    assert created.created
    repo.archive_session(
        session_id=source.guard.session_id,
        memory_domain_id="u_local",
        closed_writer=source.guard,
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        query(
            repo,
            caller,
            "read_session_content",
            target={"kind": "SESSION", "session_id": source.guard.session_id},
            limit=-1,
        )
        .content[0]
        .value["text"]
        == "source answer"
    )
    child = query(
        repo,
        caller,
        "read_session_content",
        target={"kind": "SESSION", "session_id": created.child_session_id},
        limit=-1,
    )
    assert child.content[0].value["target"]["entry_id"] != anchor
    assert (
        repo.delete_session(
            session_id=source.guard.session_id,
            memory_domain_id="u_local",
            closed_writer=source.guard,
            required_lifecycle=None,
            deadline_monotonic=monotonic() + 30,
        )
        == "DELETED"
    )
    assert (
        query(
            repo,
            caller,
            "read_session_content",
            target={"kind": "SESSION", "session_id": created.child_session_id},
            limit=-1,
        )
        == child
    )
    assert not missing.exists()


def test_read_and_search_reject_cross_domain_and_tool_only_anchor(repo):
    from tests.support.history_tools import saved_calls, call_block

    caller, source = new_session(repo), new_session(repo)
    tool = saved_calls(
        repo, source, (call_block("exec_process", {"command": "hidden tool args"}),)
    )
    with pytest.raises(SessionQueryError):
        query(
            repo,
            caller,
            "read_session_content",
            target={"kind": "SESSION_ENTRY", "entry_id": tool},
            limit=1,
        )
    other = repo.acquire_host_writer(
        intent="NEW",
        session_id=identity("session"),
        workspace_id=identity("workspace"),
        memory_domain_id="another_domain",
        writer_owner_id=identity("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )
    _, _, anchor = turn(repo, other.guard, "private-domain", answer="private")
    for target in (
        {"kind": "SESSION", "session_id": other.guard.session_id},
        {"kind": "SESSION_ENTRY", "entry_id": anchor},
    ):
        with pytest.raises(SessionQueryError):
            query(repo, caller, "read_session_content", target=target, limit=-1)


def test_direct_query_uses_physical_owner_cancel_settlement_and_root_surface(tmp_path):
    import asyncio
    from dataclasses import replace
    from threading import Event
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.conversation_kernel.tool_policy import (
        DefaultToolDispatchAuthorizationPolicy,
    )
    from pulsara_agent.conversation_kernel.content_parts import ResultPart
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES
    from pulsara_agent.ports.tool_execution import ToolBusinessValue
    from tests.support.model_config import scheduled_task_service
    from tests.support.round3 import (
        direct_tool_invocation_context,
        prepare_test_direct_tool_surface,
    )

    started, release = Event(), Event()

    class Query:
        failure = None

        def invoke(self, operation, arguments, *, current_range, deadline_monotonic):
            assert current_range == SessionContentRange(0)
            if self.failure:
                raise self.failure
            started.set()
            assert release.wait(5)
            return ToolBusinessValue(
                {}, (ResultPart("text", "complete frozen result"),)
            )

    store = Query()

    async def run():
        session = identity("session")
        port = DirectKernelToolPort(
            scheduled_tasks=scheduled_task_service(),
            workspace_root=tmp_path,
            host_owner_id=identity("host"),
            session_id=session,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            session_content_query=store,
        )
        attempt, owner, entry = identity("attempt"), identity("turn"), identity("entry")
        borrow, context = direct_tool_invocation_context(
            port,
            session_id=session,
            tool_name="read_session_content",
            tool_call_id="external-call",
            attempt_id=attempt,
            turn_id=owner,
            assistant_entry_id=entry,
        )
        context = replace(context, session_content_range=SessionContentRange(0))

        async def invoke():
            result = await port.invoke(
                tool_name="read_session_content",
                arguments={
                    "target": {"kind": "SESSION", "session_id": session},
                    "limit": -1,
                },
                tool_call_id="external-call",
                attempt_id=attempt,
                turn_id=owner,
                assistant_entry_id=entry,
                invocation_context=context,
            )
            from pulsara_agent.ports.session_content import session_query_result_schema
            from tests.support.tool_result_schemas import validate_business_result

            validate_business_result(session_query_result_schema("read_session_content"),
                result.content, result.state)
            return result

        try:
            child = prepare_test_direct_tool_surface(
                port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT
            )
            assert SESSION_QUERY_TOOL_NAMES.isdisjoint(
                spec.name for spec in child.model_surface.tool_specs
            )
            operation = asyncio.create_task(invoke())
            assert await asyncio.to_thread(started.wait, 5)
            assert port._physical_io._active
            operation.cancel()
            await asyncio.sleep(0)
            release.set()
            result = await operation
            assert result.state == "SUCCESS" and result.caller_cancelled_while_running
            assert result.content.content == (
                ResultPart("text", "complete frozen result"),
            )
            assert result.physical_timing == "LATE_AFTER_WATCHDOG"
            store.failure = RuntimeError("stored text is corrupt")
            failed = await invoke()
            assert failed.state == "APPLICATION_ERROR"
            assert failed.content.content == (
                ResultPart("text", "stored text is corrupt"),
            )
        finally:
            release.set()
            borrow.close()
            await port.aclose()

    asyncio.run(run())


def test_session_discovery_returns_the_complete_business_scope_past_ui_page_size(repo):
    caller = new_session(repo)
    expected = {new_session(repo).guard.session_id for _ in range(55)}
    value = query(repo, caller, "search_sessions")
    discovered = {
        part.value["target"]["session_id"]
        for part in value.content
        if part.type == "object"
    }
    assert expected <= discovered
