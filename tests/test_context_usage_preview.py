"""Context preview is disposable, read-only, and specific to the selected model."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.postgres


def test_host_context_preview_is_model_specific_and_does_not_open_or_install(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.adapters.openai.chat_completions import OpenAIChatCompletionsTransport
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.llm.model_connections import ModelConnectionId
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import (
        include_test_runtime_connections, test_model_binding, test_model_limits, test_model_runtime,
    )

    runtime = test_model_runtime(wire_api='openai_chat_completions',
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        limits=test_model_limits(total_context_tokens=1024000, max_input_tokens=1024000))
    medium = test_model_runtime(wire_api='openai_chat_completions', model_id='medium',
        connection_id=ModelConnectionId('model-connection:' + '1' * 32),
        limits=test_model_limits(total_context_tokens=512000, max_input_tokens=512000))
    small = test_model_runtime(wire_api='openai_chat_completions', model_id='small',
        connection_id=ModelConnectionId('model-connection:' + '2' * 32),
        limits=test_model_limits(total_context_tokens=256000, max_input_tokens=256000))
    include_test_runtime_connections(runtime, medium, small)
    monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'product-home'))
    monkeypatch.setattr(host_module.LocalMcpManagementService, 'load_configs', lambda *_args, **_kwargs: ())
    requests = []
    pause = {}
    original_stream = OpenAIChatCompletionsTransport.stream

    async def stream(adapter, *, call, context):
        requests.append(context.provider_wire_input_plan)
        if pause:
            pause['started'].set()
            await pause['release'].wait()
        adapter._mock_chunks = [
            {'choices': [{'index': 0, 'delta': {'content': 'a completed reply'}}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 80000, 'completion_tokens': 4}},
        ]
        async for item in original_stream(adapter, call=call, context=context):
            yield item

    monkeypatch.setattr(OpenAIChatCompletionsTransport, 'stream', stream)

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=tmp_path))
        try:
            async def read_preview():
                # Expose an available memory port whose activation would fail.
                # Neither same-model nor destination preview may enter it.
                def unexpected(*_args, **_kwargs):
                    raise AssertionError('UI preview must not activate memory or install a tool surface')
                support = type(session._runner._provider_dispatch._memory_support)
                with monkeypatch.context() as patch:
                    patch.setattr(support, 'available', property(lambda _owner: True))
                    patch.setattr(support, 'classify_trigger', unexpected)
                    patch.setattr(support, 'freeze_response_preference_source', unexpected)
                    patch.setattr(type(session._runner._provider_dispatch._tools), 'prepare_tool_surface_safe_point', unexpected)
                    return await session.read_context_usage()

            await session.update_model_call_binding(test_model_binding(runtime))
            slots_before = dict(session._input_continuity._slots)
            empty = await read_preview()
            assert empty['state'] == 'empty' and empty['input_tokens'] is None
            assert not requests and session._input_continuity._slots == slots_before
            # Exceed the small model's token budget within the prompt byte bound.
            await session.run_turn(PromptContent.text('上下文演示。' * 45000), command_id='command:preview:' + uuid4().hex)
            scope = next(iter(session._input_continuity._slots))
            cohort = session._input_continuity.current_cohort(scope)
            anchor = session._input_continuity.current_usage_anchor(scope)
            current = await read_preview()
            assert current['state'] == 'ready'
            assert current['budget_source'] == 'reported_input_anchor'
            assert current['input_tokens'] > 80000  # Completed assistant suffix is included.
            assert current['model_switch_pending'] is False
            assert current['compaction_expected'] is False
            assert session._input_continuity.current_cohort(scope) is cohort
            assert session._input_continuity.current_usage_anchor(scope) is anchor
            with monkeypatch.context() as patch:
                patch.setattr(type(session._compaction), 'is_fenced', lambda *_args, **_kwargs: True)
                compressing = await read_preview()
                assert compressing['state'] == 'compacting' and compressing['input_tokens'] is None
            await session.update_model_call_binding(test_model_binding(medium))
            direct = await read_preview()
            assert direct['state'] == 'ready' and direct['model_id'] == 'medium'
            assert direct['model_switch_pending'] is True
            assert direct['budget_source'] == 'heuristic'
            assert direct['compaction_expected'] is False
            assert direct['input_tokens'] > current['input_tokens']  # No cross-model 80k anchor.
            await session.update_model_call_binding(test_model_binding(small))
            compact = await read_preview()
            assert compact['state'] == 'ready' and compact['model_id'] == 'small'
            assert compact['input_budget_tokens'] < direct['input_budget_tokens']
            assert compact['compaction_expected'] is True
            assert compact['automatic_compaction_available'] is True
            session._compaction.policy = replace(session._compaction.policy, enabled=False)
            assert (await read_preview())['automatic_compaction_available'] is False
            # Preview never replaces the installed A epoch, calls B, or writes usage.
            assert len(requests) == 1
            assert session._input_continuity.current_cohort(scope) is cohort
            assert session._input_continuity.current_usage_anchor(scope) is anchor
            assert len((await core.read_provider_call_usage_page(session_id=session.session_id)).usages) == 1
            session._compaction.policy = replace(session._compaction.policy, enabled=True)
            await session.update_model_call_binding(test_model_binding(runtime))
            pause.update(started=asyncio.Event(), release=asyncio.Event())
            active = asyncio.create_task(session.run_turn(PromptContent.text('continue'),
                command_id='command:preview:' + uuid4().hex))
            await asyncio.wait_for(pause['started'].wait(), timeout=30)
            running = await read_preview()
            assert running['state'] == 'ready' and running['model_switch_pending'] is False
            await session.update_model_call_binding(test_model_binding(small))
            pending = await read_preview()
            assert pending['state'] == 'updating' and pending['model_id'] == 'small'
            assert pending['input_tokens'] is None and pending['model_switch_pending'] is True
            pause['release'].set()
            await asyncio.wait_for(active, timeout=30)
            after = await read_preview()
            assert after['state'] == 'ready' and after['budget_source'] == 'heuristic'
            assert after['compaction_expected'] is True
            assert len(requests) == 2
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())
