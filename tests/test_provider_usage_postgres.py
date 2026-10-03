"""Canonical usage is per logical execution and independent from visible replies."""
from dataclasses import replace
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.conversation_kernel.query import CanonicalConversationQuery
from pulsara_agent.conversation_kernel.repository import (
    ProviderCallUsageObservation, ProviderCallUsageWriteDisposition,
)
from tests.test_conversation_fork import repo as repo, new_session, turn, rows

pytestmark = pytest.mark.postgres


def observation(lease, turn_id, **fields):
    return ProviderCallUsageObservation(
        session_id=lease.guard.session_id, turn_id=turn_id,
        resolved_model_call_id='model_call:'+uuid4().hex,
        model_call_index=1, connection_id='model-connection:'+'a'*32,
        route_id='test', wire_api='openai_chat_completions',
        requested_model_id='requested', reported_model_id='alias',
        normalized_terminal_kind='COMPLETED', usage_status='partial', input_tokens=11,
        diagnostic_codes=('partial_observation',), **fields,
    )


def page(repo, sid, **kwargs):
    return CanonicalConversationQuery(repo.connection_provider).page_provider_call_usage(
        session_id=sid, deadline_monotonic=monotonic()+30, **kwargs,
    )


def test_usage_equal_repeat_conflict_and_partial_unknowns(repo):
    lease = new_session(repo)
    tid, _, _ = turn(repo, lease.guard, 'no visible usage owner needed')
    value = observation(lease, tid)
    args = dict(deadline_monotonic=monotonic()+30)
    assert repo.record_provider_call_usage(value, **args) is ProviderCallUsageWriteDisposition.INSERTED
    before = page(repo, lease.guard.session_id).usages[0]
    assert repo.record_provider_call_usage(value, **args) is ProviderCallUsageWriteDisposition.ALREADY_PRESENT
    assert repo.record_provider_call_usage(replace(value, input_tokens=12), **args) is ProviderCallUsageWriteDisposition.CONFLICT
    after = page(repo, lease.guard.session_id).usages[0]
    assert after == before
    assert after['output_tokens'] is None and after['computed_total_tokens'] is None
    assert after['input_tokens'] == 11
    assert after['conversation_scope_kind'] == 'ROOT'
    assert after['diagnostic_codes'] == ('partial_observation',)
    assert repo.record_provider_call_usage(replace(value, resolved_model_call_id='model_call:'+uuid4().hex,
        normalized_terminal_kind=None, usage_status='missing', input_tokens=None,
        diagnostic_codes=('provider_terminal_not_observed',)), **args) is ProviderCallUsageWriteDisposition.INSERTED
    assert len(page(repo, lease.guard.session_id).usages) == 2


def test_usage_composite_turn_session_fk_prevents_cross_owner(repo):
    first, second = new_session(repo), new_session(repo)
    first_tid, _, _ = turn(repo, first.guard, 'first')
    second_tid, _, _ = turn(repo, second.guard, 'second')
    value = observation(first, second_tid)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        repo.record_provider_call_usage(value, deadline_monotonic=monotonic()+30)
    assert page(repo, first.guard.session_id).usages == ()
    assert page(repo, second.guard.session_id).usages == ()
    assert repo.record_provider_call_usage(replace(value, turn_id=first_tid), deadline_monotonic=monotonic()+30) is ProviderCallUsageWriteDisposition.INSERTED


def test_usage_keyset_pages_same_timestamp_and_cold_queries(repo, stage2_migrated_postgres_database):
    lease = new_session(repo)
    tid, _, _ = turn(repo, lease.guard, 'paging')
    values = [observation(lease, tid) for _ in range(5)]
    for value in values:
        repo.record_provider_call_usage(value, deadline_monotonic=monotonic()+30)
    # Test clock collision to exercise ID tie-breaking, not production UPDATE authority.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute('UPDATE pulsara_v3.provider_call_usage SET observed_at=now() WHERE session_id=%s', (lease.guard.session_id,))
    actual = []
    cursor = {}
    while True:
        current = page(repo, lease.guard.session_id, maximum_rows=2, conversation_scope_kind='ROOT', **cursor)
        actual.extend(item['resolved_model_call_id'] for item in current.usages)
        if not current.has_more:
            break
        cursor = dict(after_observed_at=current.next_observed_at, after_resolved_model_call_id=current.next_resolved_model_call_id)
    assert actual == sorted(item.resolved_model_call_id for item in values)
    assert page(repo, lease.guard.session_id, conversation_scope_kind='SUBAGENT_TASK').usages == ()
    assert page(repo, lease.guard.session_id, maximum_rows=1024).has_more is False
    with pytest.raises(ValueError):
        page(repo, lease.guard.session_id, maximum_rows=1025)
    with pytest.raises(ValueError):
        page(repo, lease.guard.session_id, after_resolved_model_call_id=actual[0])


def test_usage_archive_keeps_and_permanent_session_deletion_cascades(repo):
    lease = new_session(repo)
    tid, _, _ = turn(repo, lease.guard, 'history')
    value = observation(lease, tid)
    repo.record_provider_call_usage(value, deadline_monotonic=monotonic()+30)
    args = dict(session_id=lease.guard.session_id, memory_domain_id='u_local', closed_writer=lease.guard, deadline_monotonic=monotonic()+30)
    assert repo.archive_session(**args) == 'ARCHIVED'
    assert page(repo, lease.guard.session_id).usages[0]['resolved_model_call_id'] == value.resolved_model_call_id
    assert repo.delete_session(**args) == 'DELETED'
    assert rows(repo, 'SELECT * FROM pulsara_v3.provider_call_usage WHERE resolved_model_call_id=%s', (value.resolved_model_call_id,)) == []


def test_usage_history_is_not_copied_as_child_executions(repo):
    from tests.test_conversation_fork import fork
    lease = new_session(repo)
    tid, _, final = turn(repo, lease.guard, 'source history')
    value = observation(lease, tid)
    repo.record_provider_call_usage(value, deadline_monotonic=monotonic()+30)
    child = fork(repo, lease.guard.session_id, final)
    assert child.created, child.public_code
    assert page(repo, child.child_session_id).usages == ()
    assert len(page(repo, lease.guard.session_id).usages) == 1


def test_real_host_three_calls_use_old_input_anchor_across_missing_usage(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    import asyncio
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.adapters.openai.chat_completions import OpenAIChatCompletionsTransport
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    runtime = test_model_runtime(wire_api="openai_chat_completions", postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    monkeypatch.setenv('PULSARA_HOME', str(tmp_path/'product-home'))
    monkeypatch.setattr(host_module.LocalMcpManagementService, 'load_configs', lambda *_args, **_kwargs: ())
    requests = []
    original_stream = OpenAIChatCompletionsTransport.stream

    async def stream(adapter, *, call, context):
        index = len(requests)
        requests.append(context.provider_wire_input_plan)
        chunks = [
            {'choices': [{'index': 0, 'delta': {'content': f'reply{index+1}'}}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
        ]
        if index != 1:
            chunks.append({'choices': [], 'usage': {'prompt_tokens': 100 + index*10, 'completion_tokens': 2}})
        adapter._mock_chunks = chunks
        async for item in original_stream(adapter, call=call, context=context):
            yield item

    monkeypatch.setattr(OpenAIChatCompletionsTransport, 'stream', stream)

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            for i in range(3):
                result = await session.run_turn(PromptContent.text(f'question{i+1}'), command_id='command:usage:'+uuid4().hex)
                assert result.final_text == f'reply{i+1}'
            assert len(requests) == 3
            first, middle, last = requests
            assert first.quote.budget_source == 'heuristic'
            assert middle.quote.budget_source == last.quote.budget_source == 'reported_input_anchor'
            assert middle.quote.anchor_model_call_id == last.quote.anchor_model_call_id
            assert last.quote.anchor_reported_input_tokens == 100
            assert last.quote.budget_input_tokens == 100 + last.quote.raw_final_wire_estimated_input_tokens - first.quote.raw_final_wire_estimated_input_tokens
            for plan in (middle, last):
                assert plan.materialization.root_policy_value == first.materialization.root_policy_value
                assert plan.materialization.tool_items == first.materialization.tool_items
                assert plan.materialization.ordered_input_items[:len(first.materialization.ordered_input_items)] == first.materialization.ordered_input_items
            history = await core.read_provider_call_usage_page(session_id=session.session_id)
            assert [row['usage_status'] for row in history.usages] == ['reported', 'missing', 'reported']
            assert [row['input_tokens'] for row in history.usages] == [100, None, 120]
            scope = session._input_continuity.current_view(next(iter(session._input_continuity._slots))).scope
            assert session._input_continuity.current_usage_anchor(scope).reported_input_tokens == 120
            from tests.dogfood.run_model_switch_handover_dogfood import _seed_completed_history
            from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy, CompactionDisposition
            session._compaction.policy = ResolvedCompactionPolicy(
                automatic_enabled=False, minimum_reclaim_tokens=1,
                maximum_recent_human_messages=1, maximum_recent_human_text_utf8_bytes=8192)
            await _seed_completed_history(session, segments=3, repetitions=1000)
            before_compaction_preview = await session.read_context_usage()
            outcome = await session.compact_context(command_id='command:usage-compact:'+uuid4().hex, force=True)
            assert outcome.disposition is CompactionDisposition.COMPACTED, outcome
            assert all(slot.usage_anchor is None for slot in session._input_continuity._slots.values())
            after_compaction_preview = await session.read_context_usage()
            assert after_compaction_preview['state'] == 'ready'
            assert after_compaction_preview['budget_source'] == 'heuristic'
            assert after_compaction_preview['input_tokens'] < before_compaction_preview['input_tokens']
            # The summary call is excluded from AGENT_MODEL_LOOP history. The
            # actual adopted successor starts with a new heuristic wire quote.
            assert len((await core.read_provider_call_usage_page(session_id=session.session_id)).usages) == 3
            result = await session.run_turn(PromptContent.text('after compaction'), command_id='command:usage:'+uuid4().hex)
            assert result.final_text == 'reply5'
            assert requests[-1].quote.budget_source == 'heuristic'
            assert len((await core.read_provider_call_usage_page(session_id=session.session_id)).usages) == 4
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())
