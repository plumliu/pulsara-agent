"""Current memory CRUD, shared retrieval and installed-observation behavior."""

from __future__ import annotations
import asyncio
from time import monotonic
from types import SimpleNamespace

import psycopg
import pytest

from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
    MemoryObservationState,
    memory_timestamp,
)
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementError,
    MemoryManagementSelection,
)
from pulsara_agent.conversation_kernel.memory.recall import PostgresMemoryQuery
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.identity import random_id
from pulsara_agent.memory.scope import (
    MemoryDomainContext,
    freeze_memory_read_context_binding,
    CTX_GLOBAL,
)
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from tests.support.postgres import verified_postgres_provider

pytestmark = pytest.mark.postgres


@pytest.fixture
def memory(stage2_migrated_postgres_database, request):
    db = stage2_migrated_postgres_database
    repo = ConversationKernelRepository(verified_postgres_provider(db.runtime_dsn))
    binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext(
            request.node.name.lower().replace("[", "_").replace("]", ""),
            "project",
            stable_project_key="/tmp/"
            + request.node.name.lower().replace("[", "_").replace("]", ""),
        ),
        host_workspace_id=random_id("workspace"),
    )
    with psycopg.connect(db.runtime_dsn) as c:
        c.execute(
            "INSERT INTO pulsara_v3.workspaces VALUES (%s,%s,'project',%s,'Memory project')",
            (
                binding.memory_domain_id,
                binding.current_project_context_id,
                "/tmp/" + request.node.name.lower().replace("[", "_").replace("]", ""),
            ),
        )
    return repo, db, binding


def create(
    memory, title="项目版本", statement="Python三点十二", kind="FACT", context=None
):
    repo, _, binding = memory
    return repo.create_memory(
        memory_domain_id=binding.memory_domain_id,
        context_id=context or CTX_GLOBAL,
        kind=kind,
        title=title,
        statement=statement,
        read_binding=binding,
        deadline_monotonic=monotonic() + 30,
    )


def change(memory, record, **kwargs):
    repo, _, binding = memory
    return repo.update_memory(
        memory_domain_id=binding.memory_domain_id,
        fact_id=record.fact_id,
        expected_updated_at=record.updated_at,
        read_binding=binding,
        deadline_monotonic=monotonic() + 30,
        **kwargs,
    )


def delete(memory, record):
    repo, _, binding = memory
    return repo.delete_memory(
        memory_domain_id=binding.memory_domain_id,
        fact_id=record.fact_id,
        expected_updated_at=record.updated_at,
        read_binding=binding,
        deadline_monotonic=monotonic() + 30,
    )


def port(memory, *, session=None):
    repo, _, binding = memory
    settings = SimpleNamespace(
        read=lambda: SimpleNamespace(
            memory_retrieval=SimpleNamespace(
                embedding_enabled=False, ranking_mode="off", history_enabled=False
            )
        )
    )
    io = KernelSessionIO()
    value = KernelMemoryToolPort(
        repository=repo,
        session_id=session or random_id("session"),
        read_binding=binding,
        embedding_config=EmbeddingBackendConfig(),
        io_owner=io,
        settings=settings,
    )
    return value, io


async def close(value, io):
    await value.aclose()
    await io.aclose(deadline_monotonic=monotonic() + 5)


def test_shared_crud_normalization_duplicates_scope_and_microsecond_conflicts(memory):
    first = create(memory, "  Cafe\u0301\r\n", "  内容\r\n  内部缩进\r\n ")
    second = create(memory, "Café", "内容\n  内部缩进")
    assert (
        first.fact_id != second.fact_id
        and first.title == "Café"
        and first.statement == "内容\n  内部缩进"
    )
    assert (
        first.value()["updated_at"].endswith("Z")
        and len(first.updated_at.split(".")[1].rstrip("Z")) == 6
    )
    untouched = change(memory, first, statement=first.statement)
    assert not untouched.changed and untouched.record.updated_at == first.updated_at
    updated = change(
        memory,
        first,
        statement="新正文",
        context_id=memory[2].current_project_context_id,
    ).record
    assert (
        updated.fact_id == first.fact_id
        and updated.recorded_at == first.recorded_at
        and updated.title == first.title
    )
    assert memory_timestamp(updated.updated_at) > memory_timestamp(first.updated_at)
    for operation in (
        lambda: change(memory, first, statement="过时覆盖"),
        lambda: delete(memory, first),
    ):
        with pytest.raises(MemoryManagementError, match="记忆已变化"):
            operation()
    delete(memory, updated)
    query = PostgresMemoryQuery(memory[0].connection_provider)
    assert (
        query.get(
            read_binding=memory[2],
            fact_id=updated.fact_id,
            deadline_monotonic=monotonic() + 30,
        )
        is None
    )
    assert (
        query.get(
            read_binding=memory[2],
            fact_id=second.fact_id,
            deadline_monotonic=monotonic() + 30,
        )
        is not None
    )


def test_text_codepoint_limits_and_preference_body_before_ordinary_validation(memory):
    record = create(memory, "答" * 128, "字" * 8000)
    assert len(record.statement) == 8000
    pref = create(memory, "先给结论", "字" * 90000, kind="RESPONSE_PREFERENCE")
    assert pref.statement == ""
    assert not change(memory, pref, statement="丢弃的新正文" * 90000).changed
    assert change(memory, pref, context_id=memory[2].current_project_context_id).changed
    for title, body in [
        ("答" * 129, "ok"),
        ("行\n内", "ok"),
        ("ok", "字" * 8001),
        ("ok", "  "),
    ]:
        with pytest.raises(ValueError):
            create(memory, title, body)


def test_transient_cannot_read_or_move_into_project(memory):
    record = create(memory)
    repo, _, binding = memory
    transient = freeze_memory_read_context_binding(
        domain=MemoryDomainContext(binding.memory_domain_id, "transient"),
        host_workspace_id=random_id("workspace"),
    )
    with pytest.raises(MemoryManagementError):
        repo.update_memory(
            memory_domain_id=binding.memory_domain_id,
            fact_id=record.fact_id,
            expected_updated_at=record.updated_at,
            context_id=binding.current_project_context_id,
            read_binding=transient,
            deadline_monotonic=monotonic() + 30,
        )
    project = create(memory, context=binding.current_project_context_id)
    assert (
        PostgresMemoryQuery(repo.connection_provider).get(
            read_binding=transient,
            fact_id=project.fact_id,
            deadline_monotonic=monotonic() + 30,
        )
        is None
    )


def test_title_and_statement_bm25_and_user_page_share_current_objects(memory):
    first = create(memory, "uniquetitlezebra", "ordinary body")
    repo, _, binding = memory
    query = PostgresMemoryQuery(repo.connection_provider)
    assert first.fact_id in {
        item.fact_id
        for item in query.search(
            read_binding=binding,
            query="uniquetitlezebra",
            limit=75,
            deadline_monotonic=monotonic() + 30,
        ).facts
    }
    selection = MemoryManagementSelection()
    page = repo.memory_management_catalog(
        memory_domain_id=binding.memory_domain_id,
        selection=selection,
        search="uniquetitlezebra",
        deadline_monotonic=monotonic() + 30,
    )
    assert page["items"] == [first.value()]
    assert (
        repo.memory_management_detail(
            memory_domain_id=binding.memory_domain_id,
            selection=selection,
            fact_id=first.fact_id,
            deadline_monotonic=monotonic() + 30,
        )
        == first.value()
    )
    projects = repo.memory_management_projects(
        memory_domain_id=binding.memory_domain_id, deadline_monotonic=monotonic() + 30
    )
    assert binding.current_project_context_id in {
        item["workspace_id"] for item in projects["items"]
    }


def test_manage_requires_actual_installed_or_saved_snapshot_not_latest_database(memory):
    record = create(memory)

    async def run():
        value, io = port(memory)
        context = SimpleNamespace(
            session_id=value._session_id,
            conversation_scope_kind="ROOT",
            memory_context=FrozenModelCallMemoryContext(),
        )
        args = {
            "target": record.value()["target"],
            "action": "edit",
            "statement": "changed",
        }
        try:
            rejected = await value.invoke(
                tool_name="manage_memory", arguments=args, invocation_context=context
            )
            assert (
                rejected.public_value()["meta"]["status"] == "ERROR"
                and "先用query_memory" in rejected.public_value()["content"][0]["value"]
            )
            value.bind_installed_observation_reader(
                lambda: MemoryObservationState(recall=(record.value(),))
            )
            result = await value.invoke(
                tool_name="manage_memory", arguments=args, invocation_context=context
            )
            assert result.public_value()["meta"]["status"] == "SUCCESS"
            assert result.public_value()["target"] == record.value()["target"]
            assert memory_timestamp(
                result.public_value()["meta"]["updated_at"]
            ) > memory_timestamp(record.updated_at)
            conflict = await value.invoke(
                tool_name="manage_memory",
                arguments={**args, "statement": "cannot replace"},
                invocation_context=context,
            )
            assert (
                conflict.public_value()["meta"]["status"] == "ERROR"
                and "已变化" in conflict.public_value()["content"][0]["value"]
            )
        finally:
            await close(value, io)

    asyncio.run(run())


def test_observation_preferences_append_replace_and_clear_only_after_install(memory):
    async def run():
        value, io = port(memory)
        try:
            first = await value.prepare_memory_observation(
                "简短", previous_state=MemoryObservationState()
            )
            assert (
                "当前完整回答偏好" in first.body[0].text and "[]" in first.body[0].text
            )
            state = first.next_state
            assert not (
                await value.prepare_memory_observation("简短", previous_state=state)
            ).body
            pref = create(memory, "先结论", "ignored", kind="RESPONSE_PREFERENCE")
            added = await value.prepare_memory_observation("简短", previous_state=state)
            assert (
                "新增回答偏好" in added.body[0].text
                and pref.title in added.body[0].text
            )
            assert state.preferences == ()  # preparing does not install P
            state = added.next_state
            delete(memory, pref)
            cleared = await value.prepare_memory_observation(
                "简短", previous_state=state
            )
            assert (
                "当前完整回答偏好" in cleared.body[0].text
                and cleared.next_state.preferences == ()
            )
            assert not (
                await value.prepare_memory_observation(
                    "简短", previous_state=cleared.next_state
                )
            ).body
        finally:
            await close(value, io)

    asyncio.run(run())


def test_preferences_select_eight_newest_across_global_and_project(memory):
    records = [
        create(
            memory,
            f"偏好{i}",
            "ignored",
            kind="RESPONSE_PREFERENCE",
            context=CTX_GLOBAL if i % 2 else memory[2].current_project_context_id,
        )
        for i in range(12)
    ]
    query = PostgresMemoryQuery(memory[0].connection_provider)
    selected = query.response_preference_snapshot(
        read_binding=memory[2], deadline_monotonic=monotonic() + 30
    )
    assert [item.fact_id for item in selected] == [
        item.fact_id for item in reversed(records[-8:])
    ]
    assert all(item.statement == "" for item in selected)


def test_observation_accumulates_recall_and_notifies_changes_outside_current_top_five(
    memory,
):
    records = [create(memory, f"项目事实{i}", f"正文{i}") for i in range(7)]

    async def run():
        value, io = port(memory)
        selected = [record.value() for record in records[:5]]

        async def mixed(*args):
            return tuple(selected)

        value._mixed_candidates = mixed
        try:
            first = await value.prepare_memory_observation(
                "请找相关的项目背景", previous_state=MemoryObservationState()
            )
            state = first.next_state
            assert len(state.recall) == 5
            selected[:] = [record.value() for record in records[2:7]]
            second = await value.prepare_memory_observation(
                "换一个新的相关问题", previous_state=state
            )
            assert (
                len(second.next_state.recall) == 7
                and records[0].title not in second.body[0].text
            )
            state = second.next_state
            delete(memory, records[0])
            updated = change(memory, records[1], statement="新版但未入选").record
            third = await value.prepare_memory_observation(
                "当前仍只选后五项", previous_state=state
            )
            text = third.body[0].text
            assert (
                records[0].title in text
                and "已删除" in text
                and records[1].title in text
                and "已修改" in text
            )
            assert "新版但未入选" not in text and len(third.next_state.recall) == 5
            fourth = await value.prepare_memory_observation(
                "当前仍只选后五项", previous_state=third.next_state
            )
            assert not fourth.body
            selected[:] = [updated.value()]
            fifth = await value.prepare_memory_observation(
                "本次相关查询选到了新版本", previous_state=fourth.next_state
            )
            assert (
                "新版但未入选" in fifth.body[0].text
                and "已修改" not in fifth.body[0].text
            )
            assert len(fifth.next_state.recall) == 6
        finally:
            await close(value, io)

    asyncio.run(run())


def test_required_storage_failure_emits_no_partial_increment_and_preserves_state(
    memory,
):
    pref = create(memory, "简短回答", "", kind="RESPONSE_PREFERENCE")

    async def run():
        value, io = port(memory)
        previous = MemoryObservationState(preferences=(pref.value(),))

        def failed(**kwargs):
            raise OSError("actual read failure")

        value._query.current_records = failed
        try:
            result = await value.prepare_memory_observation(
                "简短", previous_state=previous
            )
            assert (
                result.next_state is None
                and "actual read failure" in result.body[0].text
            )
            assert "response_preferences:" not in result.body[0].text
            assert previous.preferences[0]["title"] == pref.title
        finally:
            await close(value, io)

    asyncio.run(run())


def saved_observation(
    memory, *, parts=(), fields=None, session=None, tool="query_memory"
):
    from psycopg.types.json import Json, Jsonb
    from tests.test_hardcut_schema import _turn, _part, DIGEST

    repo, db, binding = memory
    session = session or random_id("session")
    workspace = binding.current_project_context_id
    assistant, context, call, attempt, result, entry = (
        random_id(kind)
        for kind in ("entry", "binding", "call", "attempt", "result", "entry")
    )
    with psycopg.connect(db.runtime_dsn) as c:
        if (
            c.execute(
                "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s", (session,)
            ).fetchone()
            is None
        ):
            c.execute(
                "INSERT INTO pulsara_v3.sessions (id,workspace_id,memory_domain_id,lifecycle,writer_generation) VALUES (%s,%s,%s,'OPEN',1)",
                (session, workspace, binding.memory_domain_id),
            )
        running = c.execute(
            "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'",
            (session,),
        ).fetchone()
        turn = running[0] if running else _turn(c, session, workspace)[0]
        sequence = c.execute(
            "SELECT max(entry_sequence) FROM pulsara_v3.transcript_entries WHERE session_id=%s",
            (session,),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO pulsara_v3.turn_context_binding_revisions (id,session_id,turn_id,revision_ordinal,base_kind,source_through_sequence) VALUES (%s,%s,%s,(SELECT coalesce(max(revision_ordinal),-1)+1 FROM pulsara_v3.turn_context_binding_revisions WHERE turn_id=%s),'FULL_HISTORY',0)",
            (context, session, turn, turn),
        )
        c.execute(
            "INSERT INTO pulsara_v3.transcript_entries (id,origin_entry_id,session_id,workspace_id,entry_owner_kind,turn_id,entry_sequence,entry_kind,context_binding_revision_id,provider_input_through_sequence) VALUES (%s,%s,%s,%s,'EXECUTED_TURN',%s,%s,'ASSISTANT_TOOL_REQUEST',%s,%s)",
            (
                assistant,
                assistant,
                session,
                workspace,
                turn,
                sequence + 1,
                context,
                sequence,
            ),
        )
        _part(
            c,
            session,
            workspace,
            entry=assistant,
            type="tool_call",
            inline_value=None,
            tool_call_id=call,
            tool_name=tool,
            tool_arguments=Jsonb({}),
        )
        invocation = random_id("call")
        c.execute(
            "INSERT INTO pulsara_v3.tool_invocations (id,session_id,turn_id,source_kind,assistant_entry_id,tool_call_id) VALUES (%s,%s,%s,'MODEL_TOOL_CALL',%s,%s)",
            (invocation, session, turn, assistant, call),
        )
        c.execute(
            "INSERT INTO pulsara_v3.tool_execution_attempts (id,session_id,invocation_id,assistant_entry_id,tool_call_id,authorization_kind,authorization_reference,permission_snapshot_fingerprint,actor_kind,actor_id) VALUES (%s,%s,%s,%s,%s,'policy','allow',%s,'runtime','test')",
            (attempt, session, invocation, assistant, call, DIGEST),
        )
        c.execute(
            "INSERT INTO pulsara_v3.transcript_entries (id,origin_entry_id,session_id,workspace_id,entry_owner_kind,turn_id,entry_sequence,entry_kind) VALUES (%s,%s,%s,%s,'EXECUTED_TURN',%s,%s,'TOOL_RESULT')",
            (entry, entry, session, workspace, turn, sequence + 2),
        )
        c.execute(
            "INSERT INTO pulsara_v3.tool_results (id,session_id,workspace_id,invocation_id,tool_call_entry_id,tool_call_id,attempt_id,result_record_kind,result_origin_kind,permission_snapshot_fingerprint,result_entry_id,result_state,observed_at,observation_origin_kind,public_fields) VALUES (%s,%s,%s,%s,%s,%s,%s,'EXECUTED','PHYSICAL_ATTEMPT',%s,%s,'SUCCESS',clock_timestamp(),'BUILTIN',%s)",
            (
                result,
                session,
                workspace,
                invocation,
                assistant,
                call,
                attempt,
                DIGEST,
                entry,
                Json(fields or {}),
            ),
        )
        for ordinal, record in enumerate(parts):
            _part(
                c,
                session,
                workspace,
                entry=entry,
                ordinal=ordinal,
                type="object",
                inline_value=__import__("json")
                .dumps(record, ensure_ascii=False)
                .encode(),
            )
    return session


def test_complete_saved_results_supply_largest_timestamp_without_reading_pages(memory):
    first = create(memory)
    session = saved_observation(memory, parts=(first.value(),))
    updated = change(memory, first, statement="新版正文").record
    saved_observation(
        memory,
        session=session,
        fields={
            "target": updated.value()["target"],
            "meta": {"updated_at": updated.updated_at},
        },
        tool="manage_memory",
    )
    # A later-accepted result can carry an older acquired candidate. Select the
    # largest observed updated_at, never the last result's timestamp blindly.
    saved_observation(memory, session=session, parts=(first.value(),))
    stamps = memory[0].read_memory_result_timestamps(
        session_id=session,
        fact_ids=(first.fact_id,),
        deadline_monotonic=monotonic() + 30,
    )
    assert stamps[first.fact_id] == memory_timestamp(updated.updated_at)
    other = random_id("session")
    assert (
        memory[0].read_memory_result_timestamps(
            session_id=other,
            fact_ids=(first.fact_id,),
            deadline_monotonic=monotonic() + 30,
        )
        == {}
    )

    async def run():
        value, io = port(memory, session=session)
        context = SimpleNamespace(
            session_id=session,
            conversation_scope_kind="ROOT",
            memory_context=FrozenModelCallMemoryContext(),
        )
        try:
            result = await value.invoke(
                tool_name="manage_memory",
                arguments={"target": first.value()["target"], "action": "delete"},
                invocation_context=context,
            )
            assert result.public_value()["meta"]["status"] == "SUCCESS"
            assert result.public_value()["content"][0]["value"] == "已删除记忆。"
        finally:
            await close(value, io)

    asyncio.run(run())


def test_remote_ranking_gets_complete_title_statement_and_refetch_drops_changed_selection(
    memory,
):
    from pulsara_agent.retrieval.rerank.protocol import RerankResult

    first = create(memory, "sharedzebra first", "长" * 8000)
    create(memory, "sharedzebra second", "other body")

    async def run():
        value, io = port(memory)
        configured = object()
        value._settings = SimpleNamespace(
            read=lambda: SimpleNamespace(
                memory_retrieval=SimpleNamespace(
                    embedding_enabled=False,
                    ranking_mode="rerank",
                    rerank=configured,
                    history_enabled=False,
                )
            )
        )
        seen = []

        class Ranker:
            async def rerank(self, query, documents, **kwargs):
                seen.extend(documents)
                index = next(
                    i
                    for i, document in enumerate(documents)
                    if "sharedzebra first" in document
                )
                change(memory, first, statement="changed while ranking")
                return tuple(
                    RerankResult(i, 1.0 if i == index else 0.1)
                    for i in range(len(documents))
                )

        value._rerank = Ranker()
        try:
            selected = await value._search_memories("sharedzebra", limit=1)
            assert (
                selected == ()
            )  # no new lookup/refill after the selected record changes
            assert (
                first.retrieval_text in seen
                and len(first.retrieval_text.encode()) > 8192
            )
        finally:
            await close(value, io)

    asyncio.run(run())


def test_remember_related_candidates_are_complete_at_most_three_and_failure_keeps_save(
    memory,
):
    others = tuple(
        create(memory, f"candidate {index}", "字" * 8000) for index in range(5)
    )

    async def run():
        value, io = port(memory)
        context = SimpleNamespace(
            session_id=value._session_id,
            conversation_scope_kind="ROOT",
            memory_context=FrozenModelCallMemoryContext(),
        )
        try:

            async def candidates(query, *, limit):
                assert (
                    query == "title: saved title\nstatement: complete body"
                    and limit == 4
                )
                return others[:4]

            value._search_memories = candidates
            result = await value.invoke(
                tool_name="remember",
                arguments={
                    "context_kind": "GLOBAL",
                    "kind": "FACT",
                    "title": "saved title",
                    "statement": "complete body",
                },
                invocation_context=context,
            )
            payload = result.public_value()
            identifier = payload["target"]["memory_id"]
            assert result.state == "SUCCESS" and payload["meta"]["updated_at"]
            projected = [
                part["value"] for part in payload["content"] if part["type"] == "object"
            ]
            assert len(projected) == 3 and all(
                len(item["statement"]) == 8000 for item in projected
            )
            assert all(item["target"]["memory_id"] != identifier for item in projected)

            async def failed(*args, **kwargs):
                raise RuntimeError("rank endpoint unavailable")

            value._search_memories = failed
            result = await value.invoke(
                tool_name="remember",
                arguments={
                    "context_kind": "GLOBAL",
                    "kind": "FACT",
                    "title": "saved despite rank failure",
                    "statement": "body",
                },
                invocation_context=context,
            )
            payload = result.public_value()
            assert (
                result.state == "SUCCESS"
                and "rank endpoint unavailable" in payload["content"][1]["value"]
            )
            assert (
                PostgresMemoryQuery(memory[0].connection_provider)
                .get(
                    read_binding=memory[2],
                    fact_id=payload["target"]["memory_id"],
                    deadline_monotonic=monotonic() + 30,
                )
                .title
                == "saved despite rank failure"
            )
        finally:
            await close(value, io)

    asyncio.run(run())


def test_cancel_during_committed_create_preserves_known_effect(memory):
    from threading import Event

    async def run():
        value, io = port(memory)
        started = Event()
        release = Event()
        real_create = memory[0].create_memory

        def held_create(**kwargs):
            record = real_create(**kwargs)
            started.set()
            assert release.wait(timeout=10)
            return record

        value._repository = SimpleNamespace(create_memory=held_create)

        async def candidates(*args, **kwargs):
            return ()

        value._search_memories = candidates
        context = SimpleNamespace(
            session_id=value._session_id,
            conversation_scope_kind="ROOT",
            memory_context=FrozenModelCallMemoryContext(),
        )
        task = asyncio.create_task(
            value.invoke(
                tool_name="remember",
                arguments={
                    "context_kind": "GLOBAL",
                    "kind": "FACT",
                    "title": "committed before stop",
                    "statement": "confirmed body",
                },
                invocation_context=context,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 10)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            result = await task
            assert result.state == "SUCCESS" and result.caller_cancelled_while_running
            assert (
                PostgresMemoryQuery(memory[0].connection_provider)
                .get(
                    read_binding=memory[2],
                    fact_id=result.public_value()["target"]["memory_id"],
                    deadline_monotonic=monotonic() + 30,
                )
                .statement
                == "confirmed body"
            )
        finally:
            release.set()
            await close(value, io)

    asyncio.run(run())


@pytest.mark.parametrize("scope,policy", [("SUBAGENT", None), ("ROOT", "disabled")])
def test_memory_native_authority_rejections_do_not_write(memory, scope, policy):
    from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy

    async def run():
        value, io = port(memory)
        context = SimpleNamespace(
            session_id=value._session_id,
            conversation_scope_kind=scope,
            memory_context=FrozenModelCallMemoryContext(
                memory_use_policy=MemoryUsePolicy.ALL_DISABLED_BY_USER
            )
            if policy
            else FrozenModelCallMemoryContext(),
        )
        try:
            result = await value.invoke(
                tool_name="remember",
                arguments={
                    "context_kind": "GLOBAL",
                    "kind": "FACT",
                    "title": "denied",
                    "statement": "body",
                },
                invocation_context=context,
            )
            assert result.state == "PERMISSION_DENIED"
            assert not memory[0].memory_management_catalog(
                memory_domain_id=memory[2].memory_domain_id,
                selection=MemoryManagementSelection(),
                deadline_monotonic=monotonic() + 30,
            )["items"]
        finally:
            await close(value, io)

    asyncio.run(run())
