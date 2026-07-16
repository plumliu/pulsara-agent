import asyncio
from pathlib import Path

import pytest

from pulsara_agent.event import (
    CustomEvent,
    EventContext,
    EventType,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RunEndEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.adapters.mock import MockTransport
from pulsara_agent.llm.adapters.openai.chat_completions import (
    ChatToolCallAccumulator,
    OpenAIChatCompletionsTransport,
    build_chat_completions_payload,
    translate_chat_completion_chunk,
)
from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_CHAT_COMPLETIONS_API,
    OPENAI_RESPONSES_API,
    provider_error_data,
)
from pulsara_agent.llm.adapters.openai.events import AgentEventBuilder
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    build_responses_payload,
    response_to_agent_events,
    translate_responses_event,
)
from pulsara_agent.llm.config import LLMConfig
from tests.support import (
    bind_test_context,
    resolve_test_call,
    test_llm_config,
    test_llm_context,
    test_model_limits,
    make_test_run_execution_activation,
)
from tests.support.runtime_session import in_memory_runtime_session
from tests.conftest import open_test_root_rollout_run
from pulsara_agent.llm.factory import build_llm_runtime
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, ToolSpec
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import (
    ModelIdentityPolicy,
    ProviderProfile,
    ThinkingProfile,
    ThinkingReplayPolicy,
)
from pulsara_agent.llm.registry import (
    LLMTransportBindingUntrusted,
    LLMTransportRegistry,
)
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.runtime import (
    LLMRuntime,
    SemanticBatchTargetPolicy,
)
from pulsara_agent.llm.commit import (
    ModelStreamCommitContractError,
    RuntimeSessionModelStreamEventCommitPort,
)
from pulsara_agent.llm.control import RunModelCallControlOwner
from pulsara_agent.llm.control import ModelCallControlResolutionError
from pulsara_agent.event_log import (
    EventIdConflict,
    FrozenEventWriteCandidate,
    decode_event_write_candidate,
)
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.sanitizing_transport import SanitizingLLMTransport
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.materialize import (
    materialize_committed_model_call_result_from_terminal_projection,
)
from pulsara_agent.primitives.model_call import (
    ModelCallControlDisposition,
    ModelCallPurpose,
    RunTerminationIntentAttributionFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.run_lifecycle import (
    RunStopReason,
    RunTerminalizationKind,
)
from pulsara_agent.runtime.state import LoopState


EVENT_CONTEXT = EventContext(
    run_id="run:test", turn_id="turn:test", reply_id="reply:test"
)


async def no_retry_sleep(_delay: float) -> None:
    return None


def _start_test_stream(
    runtime: LLMRuntime,
    *,
    call,
    context: LLMContext,
    event_context: EventContext,
    runtime_session,
    run_execution_activation=None,
    commit_port=None,
):
    lifecycle_kind = (
        "main_assistant_reply"
        if context.model_call_index is not None
        else "direct_internal_call"
    )
    if lifecycle_kind == "main_assistant_reply":
        open_test_root_rollout_run(
            runtime_session,
            event_context=event_context,
            model_target=call.target.fact,
        )
    bundle = prepare_model_lifecycle_start_bundle(
        call=call,
        context=context,
        event_context=event_context,
        runtime_session=runtime_session,
        lifecycle_kind=lifecycle_kind,
        run_execution_activation=run_execution_activation,
    )
    return runtime.start_stream(
        call=call,
        context=context,
        event_context=event_context,
        start_bundle=bundle,
        commit_port=(
            commit_port
            if commit_port is not None
            else RuntimeSessionModelStreamEventCommitPort(
                runtime_session=runtime_session,
                state=None,
            )
        ),
        execution_registry=runtime_session.model_stream_execution_registry,
    )


async def collect_events(runtime: LLMRuntime, role: ModelRole, context: LLMContext):
    target = runtime.resolve_target(role=role, requested_options=None)
    call = runtime.resolve_call(
        target=target, purpose=ModelCallPurpose.AGENT_MODEL_LOOP
    )
    context = bind_test_context(call, context)
    session = in_memory_runtime_session(Path.cwd())
    handle = _start_test_stream(runtime,
        call=call,
        context=context,
        event_context=EVENT_CONTEXT,
        runtime_session=session,
        run_execution_activation=(
            make_test_run_execution_activation()
            if context.model_call_index is not None
            else None
        ),
    )
    completion = await handle.wait_completed()
    return list(completion.committed_events)


async def collect_transport_events(transport, config, role, context):
    call = resolve_test_call(config, role=role, transport=transport)
    return [
        event
        async for event in transport.stream(
            call=call,
            context=bind_test_context(call, context),
            event_context=EVENT_CONTEXT,
        )
    ]


async def _completed_control_fixture(tmp_path):
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="mock",
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="control result"))
    runtime = LLMRuntime(config=config, registry=registry)
    session = in_memory_runtime_session(tmp_path)
    target = runtime.resolve_target(role=ModelRole.FLASH)
    call = runtime.resolve_call(
        target=target,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    context = bind_test_context(
        call,
        test_llm_context(
            messages=(LLMMessage.user("Say hi"),),
            context_id="context:control",
            model_call_index=1,
        ),
    )
    activation = make_test_run_execution_activation()
    handle = _start_test_stream(runtime,
        call=call,
        context=context,
        event_context=EVENT_CONTEXT,
        runtime_session=session,
        run_execution_activation=activation,
    )
    result = await handle.wait_result()
    owner = RunModelCallControlOwner(
        run_id=EVENT_CONTEXT.run_id,
        activation=activation,
        segment_id="segment:test:1",
        segment_generation=activation.segment_generation,
    )
    return (
        session,
        result,
        owner,
        LoopState(
            session_id=session.runtime_session_id,
            run_id=EVENT_CONTEXT.run_id,
        ),
        activation,
    )


def _termination_intent(activation) -> RunTerminationIntentAttributionFact:
    payload = {
        "schema_version": "run_termination_intent_attribution.v1",
        "intent_id": "termination-intent:test",
        "kind": "user_stop",
        "requested_at_utc": "2026-07-14T00:00:00Z",
        "requester_id": "host-session:test",
        "target_run_execution_activation_fingerprint": (
            activation.activation_fingerprint
        ),
    }
    return RunTerminationIntentAttributionFact(
        **payload,
        attribution_fingerprint=sha256_fingerprint(
            "run-termination-intent-attribution:v1", payload
        ),
    )


def payload_call(config, *, role=ModelRole.PRO, options=None, api="responses"):
    transport = (
        OpenAIChatCompletionsTransport(api_key="sk-test")
        if api == "chat"
        else OpenAIResponsesTransport(api_key="sk-test")
    )
    return resolve_test_call(config, role=role, options=options, transport=transport)


def test_config_resolves_pro_and_flash_models() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="provider/pro-model",
        flash_model="provider/flash-model",
    )

    pro = config.model_for(ModelRole.PRO)
    flash = config.model_for(ModelRole.FLASH)

    assert pro.id == "provider/pro-model"
    assert pro.role is ModelRole.PRO
    assert flash.id == "provider/flash-model"
    assert flash.role is ModelRole.FLASH
    assert pro.api == "openai_responses"


def test_runtime_streams_agent_events_through_registered_transport() -> None:
    import asyncio

    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="mock",
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="hello"))
    runtime = LLMRuntime(config=config, registry=registry)

    events = asyncio.run(
        collect_events(
            runtime,
            ModelRole.FLASH,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:test",
                model_call_index=3,
            ),
        )
    )

    assert isinstance(events[0], ReplyStartEvent)
    assert isinstance(events[1], RolloutBudgetReservationCreatedEvent)
    assert isinstance(events[2], ModelCallStartEvent)
    assert events[2].resolved_call.target.model_id == "flash"
    assert events[2].context_id == "context:test"
    assert events[2].model_call_index == 3
    assert isinstance(events[3], TextBlockStartEvent)
    assert isinstance(events[4], TextBlockDeltaEvent)
    assert events[4].block_id == events[3].block_id
    assert events[4].delta == "hello"
    assert isinstance(events[5], TextBlockEndEvent)
    assert isinstance(events[6], ModelCallTerminalProjectionCommittedEvent)
    assert isinstance(events[7], ModelCallEndEvent)
    assert events[7].terminal_projection is not None
    assert (
        events[7].terminal_projection.projection_reference
        == events[6].projection_reference
    )
    assert isinstance(events[8], RolloutBudgetReservationSettledEvent)
    assert isinstance(events[9], ReplyEndEvent)


def test_runtime_batches_model_semantic_deltas_before_durable_commit(tmp_path) -> None:
    class BurstTransport:
        api = "mock"
        binding_id = "test.burst"
        contract_version = "v1"

        async def stream(self, *, call, context, event_context):
            del call, context
            block_id = "text:burst"
            yield TextBlockStartEvent(
                **event_context.event_fields(), block_id=block_id
            )
            for _ in range(40):
                yield TextBlockDeltaEvent(
                    **event_context.event_fields(), block_id=block_id, delta="x"
                )
            yield TextBlockEndEvent(
                **event_context.event_fields(), block_id=block_id
            )

    class RecordingCommitPort(RuntimeSessionModelStreamEventCommitPort):
        def __init__(self, *, runtime_session):
            super().__init__(runtime_session=runtime_session, state=None)
            self.semantic_batch_sizes: list[int] = []

        async def commit_semantic(self, candidates, *, guard, live_cursor):
            self.semantic_batch_sizes.append(len(candidates))
            return await super().commit_semantic(
                candidates,
                guard=guard,
                live_cursor=live_cursor,
            )

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(BurstTransport())
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        port = RecordingCommitPort(runtime_session=session)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:semantic-batch",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
            commit_port=port,
        )

        completion = await handle.wait_completed()

        assert completion.terminal_outcome == "completed"
        assert sum(port.semantic_batch_sizes) == 42
        assert len(port.semantic_batch_sizes) < 10
        assert max(port.semantic_batch_sizes) > 1

    asyncio.run(scenario())


def test_runtime_accepts_an_explicit_semantic_batch_policy(tmp_path) -> None:
    class BurstTransport:
        api = "mock"
        binding_id = "test.explicit-batch-policy"
        contract_version = "v1"

        async def stream(self, *, call, context, event_context):
            del call, context
            block_id = "text:explicit-policy"
            yield TextBlockStartEvent(
                **event_context.event_fields(), block_id=block_id
            )
            for _ in range(10):
                yield TextBlockDeltaEvent(
                    **event_context.event_fields(),
                    block_id=block_id,
                    delta="x",
                )
            yield TextBlockEndEvent(
                **event_context.event_fields(), block_id=block_id
            )

    class RecordingCommitPort(RuntimeSessionModelStreamEventCommitPort):
        def __init__(self, *, runtime_session):
            super().__init__(runtime_session=runtime_session, state=None)
            self.semantic_batch_sizes: list[int] = []

        async def commit_semantic(self, candidates, *, guard, live_cursor):
            self.semantic_batch_sizes.append(len(candidates))
            return await super().commit_semantic(
                candidates,
                guard=guard,
                live_cursor=live_cursor,
            )

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(BurstTransport())
        runtime = LLMRuntime(
            config=config,
            registry=registry,
            semantic_batch_policy=SemanticBatchTargetPolicy(
                max_events=4,
                flush_structural_events=False,
            ),
        )
        session = in_memory_runtime_session(tmp_path)
        port = RecordingCommitPort(runtime_session=session)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:explicit-semantic-batch",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
            commit_port=port,
        )

        completion = await handle.wait_completed()

        assert completion.terminal_outcome == "completed"
        assert port.semantic_batch_sizes == [4, 4, 4]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "policy",
    [
        SemanticBatchTargetPolicy(max_events=17),
        SemanticBatchTargetPolicy(max_chars=4_097),
        SemanticBatchTargetPolicy(max_age_seconds=0.026),
    ],
)
def test_runtime_rejects_semantic_batch_targets_above_hard_limits(
    policy,
) -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="mock",
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport("ok"))

    with pytest.raises(ValueError, match="semantic batch target exceeds"):
        LLMRuntime(
            config=config,
            registry=registry,
            semantic_batch_policy=policy,
        )


def test_semantic_batch_age_deadline_flushes_during_provider_stall(tmp_path) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        delta_committed = asyncio.Event()

        class StallingTransport:
            api = "mock"
            binding_id = "test.stalling"
            contract_version = "v1"

            async def stream(self, *, call, context, event_context):
                del call, context
                block_id = "text:stall"
                yield TextBlockStartEvent(
                    **event_context.event_fields(), block_id=block_id
                )
                yield TextBlockDeltaEvent(
                    **event_context.event_fields(), block_id=block_id, delta="x"
                )
                await release.wait()
                yield TextBlockEndEvent(
                    **event_context.event_fields(), block_id=block_id
                )

        class ObservingCommitPort(RuntimeSessionModelStreamEventCommitPort):
            async def commit_semantic(self, candidates, *, guard, live_cursor):
                decoded = tuple(
                    decode_event_write_candidate(candidate)
                    for candidate in candidates
                )
                result = await super().commit_semantic(
                    candidates,
                    guard=guard,
                    live_cursor=live_cursor,
                )
                if any(isinstance(event, TextBlockDeltaEvent) for event in decoded):
                    delta_committed.set()
                return result

        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(StallingTransport())
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:semantic-age-deadline",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
            commit_port=ObservingCommitPort(
                runtime_session=session,
                state=None,
            ),
        )
        await asyncio.wait_for(delta_committed.wait(), timeout=0.5)
        assert handle.completion.done() is False
        release.set()
        completion = await asyncio.wait_for(handle.wait_completed(), timeout=1)
        assert completion.terminal_outcome == "completed"

    asyncio.run(scenario())


def test_session_owned_model_stream_persists_before_notifying(tmp_path) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="owned"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:owned",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        observed = [event async for event in handle.subscribe()]
        completion = await handle.wait_completed()

        assert completion.terminal_outcome == "completed"
        all_events = tuple(session.event_log.iter())
        assert all_events[-len(completion.committed_events) :] == (
            completion.committed_events
        )
        assert tuple(observed) == completion.committed_events
        assert [event.sequence for event in observed] == list(
            range(observed[0].sequence, observed[-1].sequence + 1)
        )

    asyncio.run(scenario())


def test_main_model_start_freezes_event_safe_run_activation_in_recovery_plan(
    tmp_path,
) -> None:
    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="owned"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:activation",
                model_call_index=1,
            ),
        )
        activation = make_test_run_execution_activation()
        open_test_root_rollout_run(
            session,
            event_context=EVENT_CONTEXT,
            model_target=call.target.fact,
        )
        bundle = prepare_model_lifecycle_start_bundle(
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            lifecycle_kind="main_assistant_reply",
            run_execution_activation=activation,
        )

        assert bundle.recovery_plan.run_execution_activation == activation
        assert bundle.recovery_plan.control_downstream_predicate_contract is not None
        assert bundle.rollout_accounting_mode == "root_account"
        assert bundle.expected_rollout_account_state_fingerprint is not None
        assert all(
            isinstance(candidate, FrozenEventWriteCandidate)
            for candidate in bundle.companion_candidates
        )
        assert [
            type(decode_event_write_candidate(candidate))
            for candidate in bundle.companion_candidates
        ] == [
            ReplyStartEvent,
            RolloutBudgetReservationCreatedEvent,
        ]

    asyncio.run(scenario())


def test_direct_model_start_has_no_reply_reservation_or_run_activation(tmp_path) -> None:
    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="direct"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.MEMORY_REFLECTION,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Reflect"),),
                context_id="context:direct",
                model_call_index=None,
            ),
        )
        bundle = prepare_model_lifecycle_start_bundle(
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            lifecycle_kind="direct_internal_call",
        )
        handle = runtime.start_stream(
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            start_bundle=bundle,
            commit_port=RuntimeSessionModelStreamEventCommitPort(
                runtime_session=session,
                state=None,
            ),
            execution_registry=session.model_stream_execution_registry,
        )
        completion = await handle.wait_completed()

        assert bundle.companion_candidates == ()
        assert bundle.rollout_accounting_mode == "not_rollout_accounted"
        assert bundle.recovery_plan.run_execution_activation is None
        assert bundle.recovery_plan.control_downstream_predicate_contract is None
        assert not any(
            isinstance(
                event,
                (
                    ReplyStartEvent,
                    ReplyEndEvent,
                    RolloutBudgetReservationCreatedEvent,
                    RolloutBudgetReservationSettledEvent,
                ),
            )
            for event in completion.committed_events
        )

    asyncio.run(scenario())


def test_model_call_start_allows_noop_ledger_progress_after_rollout_preparation(
    tmp_path,
) -> None:
    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="dispatches after noop"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:noop-account-progress",
                model_call_index=1,
            ),
        )
        activation = make_test_run_execution_activation()
        open_test_root_rollout_run(
            session,
            event_context=EVENT_CONTEXT,
            model_target=call.target.fact,
        )
        bundle = prepare_model_lifecycle_start_bundle(
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            lifecycle_kind="main_assistant_reply",
            run_execution_activation=activation,
        )
        await session.write_event(
            RunErrorEvent(
                **EVENT_CONTEXT.event_fields(),
                message="synthetic non-rollout durable fact",
                code="test_rollout_noop_progress",
            )
        )
        handle = runtime.start_stream(
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            start_bundle=bundle,
            commit_port=RuntimeSessionModelStreamEventCommitPort(
                runtime_session=session,
                state=None,
            ),
            execution_registry=session.model_stream_execution_registry,
        )

        completion = await handle.wait_completed()

        assert completion.terminal_outcome == "completed"
        assert any(
            isinstance(event, ModelCallStartEvent)
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_model_call_start_rejects_semantic_rollout_state_change_after_preparation(
    tmp_path,
) -> None:
    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="must not dispatch"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:semantic-account-drift",
                model_call_index=1,
            ),
        )
        open_test_root_rollout_run(
            session,
            event_context=EVENT_CONTEXT,
            model_target=call.target.fact,
        )
        bundle = prepare_model_lifecycle_start_bundle(
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            lifecycle_kind="main_assistant_reply",
            run_execution_activation=make_test_run_execution_activation(),
        )
        assert bundle.reservation is not None
        await session.write_event(
            RolloutBudgetReservationCreatedEvent(
                id="rollout_budget_reservation_created:test:concurrent",
                **EVENT_CONTEXT.event_fields(),
                reservation=bundle.reservation,
            )
        )
        handle = runtime.start_stream(
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            start_bundle=bundle,
            commit_port=RuntimeSessionModelStreamEventCommitPort(
                runtime_session=session,
                state=None,
            ),
            execution_registry=session.model_stream_execution_registry,
        )

        with pytest.raises(
            ModelStreamCommitContractError,
            match="state changed after preparation",
        ):
            await handle.wait_completed()
        assert not any(
            isinstance(event, ModelCallStartEvent)
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_detaching_model_stream_observer_does_not_cancel_owner(tmp_path) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="continues"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:detach",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        observer = handle.subscribe()
        closed = await observer.detach()
        completion = await handle.wait_completed()

        assert closed.close_reason == "detached_by_caller"
        assert completion.terminal_outcome == "completed"
        assert any(
            isinstance(event, ModelCallEndEvent)
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_late_subscription_catches_up_from_model_start_without_notification_gap(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="late observer"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:late-observer",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        completion = await handle.wait_completed()

        observer = handle.subscribe()
        observed = [event async for event in observer]
        closed = await observer.wait_closed()

        assert tuple(observed) == completion.committed_events
        assert closed.close_reason == "terminal_observed"
        assert closed.last_confirmed_sequence == observed[-1].sequence

    asyncio.run(scenario())


def test_subscription_break_detaches_without_stopping_transport_or_terminalization(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="owner continues"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:break-observer",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )

        observer = handle.subscribe()
        first_event = None
        async with observer:
            async for event in observer:
                first_event = event
                break
        closed = await observer.wait_closed()
        completion = await handle.wait_completed()

        assert first_event is not None
        assert closed.close_reason == "detached_by_caller"
        assert closed.last_confirmed_sequence == first_event.sequence
        assert completion.terminal_outcome == "completed"
        assert any(
            isinstance(event, ModelCallEndEvent)
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_subscription_task_cancel_detaches_without_cancelling_stream_worker(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="owner survives subscriber cancel"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:cancel-observer-task",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        observer = handle.subscribe()
        first_seen = asyncio.Event()
        hold_subscriber = asyncio.Event()

        async def consume() -> None:
            async with observer:
                await anext(observer)
                first_seen.set()
                await hold_subscriber.wait()

        consumer = asyncio.create_task(consume())
        await first_seen.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

        closed = await observer.wait_closed()
        completion = await handle.wait_completed()
        result = await handle.wait_result()

        assert closed.close_reason == "detached_by_caller"
        assert completion.terminal_outcome == "completed"
        assert result.terminal_outcome == "completed"
        assert any(
            isinstance(event, ModelCallEndEvent)
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_provider_error_terminal_waits_for_inner_physical_drain(tmp_path) -> None:
    import asyncio

    async def scenario() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        class FailingIterator:
            def __init__(self) -> None:
                self._delivered = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._delivered:
                    raise StopAsyncIteration
                self._delivered = True
                return RunErrorEvent(
                    **EVENT_CONTEXT.event_fields(),
                    code="provider_failed",
                    message="provider unavailable",
                )

            async def aclose(self) -> None:
                close_started.set()
                await allow_close.wait()

        class RawFailureTransport:
            api = "mock"
            binding_id = "test.raw-failure"
            contract_version = "v1"

            def stream(self, **_kwargs):
                return FailingIterator()

        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(RawFailureTransport())
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        call = runtime.resolve_call(
            target=runtime.resolve_target(role=ModelRole.FLASH),
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("fail"),),
                context_id="context:provider-error-drain",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )

        await close_started.wait()
        assert not any(
            isinstance(event, ModelCallEndEvent)
            for event in session.event_log.iter()
        )

        allow_close.set()
        completion = await handle.wait_completed()

        assert completion.terminal_outcome == "provider_error"
        assert any(
            isinstance(event, ModelCallEndEvent)
            and event.outcome == "provider_error"
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_physical_completion_blocked_untrusted_preserves_owner_and_forbids_terminal_commit(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        class FailingIterator:
            def __init__(self) -> None:
                self._delivered = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._delivered:
                    raise StopAsyncIteration
                self._delivered = True
                return RunErrorEvent(
                    **EVENT_CONTEXT.event_fields(),
                    code="provider_failed",
                    message="provider unavailable",
                )

            async def aclose(self) -> None:
                raise OSError("raw close failure")

        class RawFailureTransport:
            api = "mock"
            binding_id = "test.raw-blocked-drain"
            contract_version = "v1"

            def stream(self, **_kwargs):
                return FailingIterator()

        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(RawFailureTransport())
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        call = runtime.resolve_call(
            target=runtime.resolve_target(role=ModelRole.FLASH),
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("fail"),),
                context_id="context:provider-error-blocked-drain",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )

        completion = await handle.wait_completed()

        assert completion.terminal_outcome == "reconciliation_blocked"
        assert session.reconciliation_required is True
        assert session.model_stream_execution_registry.active_handle_count() == 1
        assert not any(
            isinstance(event, ModelCallEndEvent)
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_unexpected_public_wrapper_exception_before_error_uses_constant_runtime_containment_and_latches_binding(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        transport = SanitizingLLMTransport(MockTransport(text="unused"))
        registry.register(transport)
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        call = runtime.resolve_call(
            target=runtime.resolve_target(role=ModelRole.FLASH),
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("fail"),),
                context_id="context:public-wrapper-fault",
                model_call_index=1,
            ),
        )

        def fail_open_stream(*_args, **_kwargs):
            raise RuntimeError("secret-token-should-never-be-retained")

        monkeypatch.setattr(transport, "open_stream", fail_open_stream)
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )

        completion = await handle.wait_completed()
        result = await handle.wait_result()
        events = tuple(session.event_log.iter())
        model_end = next(
            event for event in events if isinstance(event, ModelCallEndEvent)
        )

        assert completion.terminal_outcome == "runtime_error"
        assert completion.diagnostic_code == "public_transport_open_stream_fault"
        assert result.terminal_outcome == "runtime_error"
        assert model_end.provider_dispatch_status == "not_started"
        assert "secret-token" not in repr(events)
        with pytest.raises(LLMTransportBindingUntrusted):
            runtime.resolve_target(role=ModelRole.FLASH)

    asyncio.run(scenario())


def test_control_disposition_publication_after_commit_folds_full_before_permit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def scenario() -> None:
        session, result, owner, state, _activation = (
            await _completed_control_fixture(tmp_path)
        )
        pending_projection = session.transcript_projection_state_store.snapshot()
        assert pending_projection.checkpointable is False
        assert pending_projection.pending_model_disposition_call_ids == (
            result.resolved_model_call_id,
        )
        event_log_type = type(session.event_log)
        original_extend = event_log_type.extend

        def commit_then_cancel(self, events, *args, **kwargs):
            original_extend(self, events, *args, **kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(event_log_type, "extend", commit_then_cancel)
        resolution = await owner.resolve_completed_call(
            result=result,
            model_call_index=1,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            state=state,
        )

        assert resolution.accepted_permit is not None
        assert await owner.permit_is_active(resolution.accepted_permit)
        durable = tuple(
            event
            for event in session.event_log.iter()
            if event.type is EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED
        )
        assert durable == (resolution.disposition_event,)
        accepted_projection = session.transcript_projection_state_store.snapshot()
        assert accepted_projection.checkpointable is True
        assert accepted_projection.pending_model_disposition_call_ids == ()

    asyncio.run(scenario())


def test_projection_authority_not_raw_semantic_stream_controls_completed_result(
    tmp_path,
) -> None:
    async def scenario() -> None:
        session, result, _owner, _state, _activation = (
            await _completed_control_fixture(tmp_path)
        )
        events = tuple(session.event_log.iter())
        drifted: list = []
        for event in events:
            if isinstance(event, TextBlockDeltaEvent):
                drifted.append(
                    TextBlockDeltaEvent.model_validate(
                        {**event.model_dump(mode="json"), "delta": "RAW-DRIFT"}
                    )
                )
            else:
                drifted.append(event)
        end = next(event for event in events if isinstance(event, ModelCallEndEvent))
        document = session.transcript_projection_document_registry.resolve(
            end.terminal_projection.projection_reference
        )

        projected = (
            materialize_committed_model_call_result_from_terminal_projection(
                tuple(drifted),
                resolved_model_call_id=result.resolved_model_call_id,
                runtime_session_id=session.runtime_session_id,
                document=document,
            )
        )

        assert projected.combined_text == "control result"
        assert projected.result_fingerprint == result.result_fingerprint
        assert "RAW-DRIFT" not in projected.combined_text

    asyncio.run(scenario())


def test_control_disposition_precommit_failure_retries_exact_stable_candidate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.runtime.session import EventCommitError

    async def scenario() -> None:
        session, result, owner, state, _activation = (
            await _completed_control_fixture(tmp_path)
        )
        session_type = type(session)
        original = session_type.commit_reduce_events_from_thread
        attempts: list[tuple[str, str]] = []

        def fail_once(self, events, **kwargs):
            candidate = events[0]
            attempts.append((candidate.id, candidate.event_fingerprint))
            if len(attempts) == 1:
                raise EventCommitError("synthetic disposition pre-commit failure")
            return original(self, events, **kwargs)

        monkeypatch.setattr(
            session_type,
            "commit_reduce_events_from_thread",
            fail_once,
        )
        resolution = await owner.resolve_completed_call(
            result=result,
            model_call_index=1,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            state=state,
        )

        assert len(attempts) == 2
        assert len(set(attempts)) == 1
        assert resolution.accepted_permit is not None
        assert session.transcript_projection_state_store.snapshot().checkpointable

    asyncio.run(scenario())


def test_uncommitted_model_disposition_blocks_run_end_and_remains_retryable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.runtime.session import EventCommitError

    async def scenario() -> None:
        session, result, owner, state, _activation = (
            await _completed_control_fixture(tmp_path)
        )
        session_type = type(session)
        attempted: list[tuple[str, str]] = []

        def fail_before_commit(self, events, **_kwargs):
            candidate = events[0]
            attempted.append((candidate.id, candidate.event_fingerprint))
            raise EventCommitError("synthetic persistent disposition outage")

        def confirm_none(self, events, **_kwargs):
            del events
            raise EventCommitError("synthetic disposition confirmation miss")

        monkeypatch.setattr(
            session_type,
            "commit_reduce_events_from_thread",
            fail_before_commit,
        )
        monkeypatch.setattr(
            session_type,
            "confirm_and_reduce_event_batch",
            confirm_none,
        )
        with pytest.raises(
            ModelCallControlResolutionError,
            match="stable model disposition remained uncommitted",
        ):
            await owner.resolve_completed_call(
                result=result,
                model_call_index=1,
                event_context=EVENT_CONTEXT,
                runtime_session=session,
                state=state,
            )

        assert len(attempted) == 3
        assert len(set(attempted)) == 1
        projection = session.transcript_projection_state_store.snapshot()
        assert projection.checkpointable is False
        assert projection.pending_model_disposition_call_ids == (
            result.resolved_model_call_id,
        )
        assert session.model_call_control_disposition_owner.pending_candidate_ids == (
            result.resolved_model_call_id,
        )
        await owner.retire()
        assert session.model_call_control_disposition_owner.pending_candidate_ids == (
            result.resolved_model_call_id,
        )
        with pytest.raises(
            ValueError,
            match="FULL model control disposition commit",
        ):
            session.write_events_from_thread(
                (
                    RunEndEvent(
                        **EVENT_CONTEXT.event_fields(),
                        status="failed",
                        stop_reason=RunStopReason.MODEL_ERROR,
                        terminalization_kind=(
                            RunTerminalizationKind.EXECUTION_FAILURE
                        ),
                        error_message="model control disposition unavailable",
                    ),
                )
            )

    asyncio.run(scenario())


def test_control_disposition_cancel_after_full_adopts_session_winner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.runtime.event_write_service import RuntimeEventWriteCancelled

    async def scenario() -> None:
        session, result, owner, state, _activation = (
            await _completed_control_fixture(tmp_path)
        )
        original_execute = session.event_write_service.execute

        async def cancel_after_full(operation, **kwargs):
            write = await original_execute(operation, **kwargs)
            raise RuntimeEventWriteCancelled(
                operation_result=write,
                operation_error=None,
                deadline_monotonic=kwargs["deadline_monotonic"],
            )

        monkeypatch.setattr(
            session.event_write_service,
            "execute",
            cancel_after_full,
        )

        with pytest.raises(asyncio.CancelledError):
            await owner.resolve_completed_call(
                result=result,
                model_call_index=1,
                event_context=EVENT_CONTEXT,
                runtime_session=session,
                state=state,
            )

        durable = tuple(
            event
            for event in session.event_log.iter()
            if event.type is EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED
        )
        assert len(durable) == 1
        assert durable[0].disposition is ModelCallControlDisposition.ACCEPTED
        assert session.model_call_control_disposition_owner.pending_candidate_count == 0
        assert session.model_call_control_disposition_owner.winner_ids == (
            result.resolved_model_call_id,
        )
        winner = session.model_call_control_disposition_owner.winner_for(
            result.resolved_model_call_id
        )
        assert winner is not None and winner.accepted_permit is not None
        assert await owner.permit_is_active(winner.accepted_permit)
        assert session.transcript_projection_state_store.snapshot().checkpointable

    asyncio.run(scenario())


def test_control_disposition_observer_failure_does_not_revoke_durable_winner_or_permit(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        session, result, owner, state, _activation = (
            await _completed_control_fixture(tmp_path)
        )

        class FailingObserver:
            async def on_published_event(self, _published) -> None:
                raise RuntimeError("synthetic control observer failure")

        session.publisher.subscribe(FailingObserver())
        resolution = await owner.resolve_completed_call(
            result=result,
            model_call_index=1,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            state=state,
        )
        for _ in range(20):
            if session.publisher.errors:
                break
            await asyncio.sleep(0)

        assert session.publisher.errors
        assert resolution.accepted_permit is not None
        assert await owner.permit_is_active(resolution.accepted_permit)
        assert resolution.disposition_event in tuple(session.event_log.iter())

    asyncio.run(scenario())


def test_control_disposition_reducer_failure_never_installs_execution_permit(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        session, result, owner, state, _activation = (
            await _completed_control_fixture(tmp_path)
        )

        def fail_fold(_events) -> None:
            raise RuntimeError("synthetic control reducer failure")

        session.register_committed_reducer(
            reducer_id="test:control-fold-failure",
            through_sequence=session.event_log.next_sequence() - 1,
            apply_committed=fail_fold,
        )
        with pytest.raises(
            RuntimeError,
            match="reducer state is untrusted",
        ):
            await owner.resolve_completed_call(
                result=result,
                model_call_index=1,
                event_context=EVENT_CONTEXT,
                runtime_session=session,
                state=state,
            )

        assert session.reconciliation_required is True
        assert any(
            event.type is EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_control_disposition_event_requires_exact_call_result_and_start_activation_join(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        session, result, _owner, state, activation = (
            await _completed_control_fixture(tmp_path)
        )
        wrong_context = EventContext(
            run_id=EVENT_CONTEXT.run_id,
            turn_id="turn:other",
            reply_id=EVENT_CONTEXT.reply_id,
        )
        owner = RunModelCallControlOwner(
            run_id=EVENT_CONTEXT.run_id,
            activation=activation,
            segment_id="segment:test",
            segment_generation=activation.segment_generation,
        )

        with pytest.raises(
            ModelCallControlResolutionError,
            match="attribution mismatch",
        ):
            await owner.resolve_completed_call(
                result=result,
                model_call_index=1,
                event_context=wrong_context,
                runtime_session=session,
                state=state,
            )

        assert not any(
            event.type is EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_control_disposition_partial_unknown_or_conflict_latches_and_blocks_execution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def scenario() -> None:
        session, result, owner, state, _activation = (
            await _completed_control_fixture(tmp_path)
        )

        def conflict(*_args, **_kwargs):
            raise EventIdConflict("model-control-conflict")

        session_type = type(session)
        monkeypatch.setattr(
            session_type,
            "commit_reduce_events_from_thread",
            conflict,
        )
        monkeypatch.setattr(
            session_type,
            "confirm_and_reduce_event_batch",
            conflict,
        )

        with pytest.raises(
            ModelCallControlResolutionError,
            match="structurally untrusted",
        ):
            await owner.resolve_completed_call(
                result=result,
                model_call_index=1,
                event_context=EVENT_CONTEXT,
                runtime_session=session,
                state=state,
            )

        assert session.reconciliation_required is True

    asyncio.run(scenario())


def test_termination_intent_wins_shared_control_lock_and_commits_suppressed_disposition(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        session, result, owner, state, activation = (
            await _completed_control_fixture(tmp_path)
        )
        intent = _termination_intent(activation)
        assert await owner.install_termination_intent(intent) == "installed"

        resolution = await owner.resolve_completed_call(
            result=result,
            model_call_index=1,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            state=state,
        )

        assert resolution.accepted_permit is None
        assert (
            resolution.disposition_event.disposition
            is ModelCallControlDisposition.SUPPRESSED_BY_TERMINATION
        )
        assert resolution.disposition_event.termination_intent == intent

    asyncio.run(scenario())


def test_accepted_first_then_later_stop_does_not_rewrite_disposition_but_cancels_downstream(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        session, result, owner, state, activation = (
            await _completed_control_fixture(tmp_path)
        )
        resolution = await owner.resolve_completed_call(
            result=result,
            model_call_index=1,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            state=state,
        )
        permit = resolution.accepted_permit
        assert permit is not None and await owner.permit_is_active(permit)

        await owner.install_termination_intent(_termination_intent(activation))

        assert not await owner.permit_is_active(permit)
        repeated = await owner.resolve_completed_call(
            result=result,
            model_call_index=1,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            state=state,
        )
        assert repeated == resolution
        assert (
            repeated.disposition_event.disposition
            is ModelCallControlDisposition.ACCEPTED
        )

    asyncio.run(scenario())


def test_subscription_close_state_records_typed_reason_last_sequence_and_terminal_cursor(
    tmp_path,
) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="cursor"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:cursor",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        completion = await handle.wait_completed()
        after_sequence = completion.committed_events[1].sequence
        assert after_sequence is not None

        observer = handle.subscribe(after_sequence=after_sequence)
        observed = [event async for event in observer]
        closed = await observer.wait_closed()
        model_end = next(
            event
            for event in completion.committed_events
            if isinstance(event, ModelCallEndEvent)
        )

        assert observed == [
            event
            for event in completion.committed_events
            if event.sequence is not None and event.sequence > after_sequence
        ]
        assert closed.close_reason == "terminal_observed"
        assert closed.last_confirmed_sequence == observed[-1].sequence
        assert closed.terminal_sequence == model_end.sequence
        assert closed.can_resume_from_cursor is False

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_phase", ("start", "semantic", "terminal"))
def test_model_stream_phase_commit_baseexception_confirms_stable_batch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    import asyncio

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="stable commit"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        session_type = type(session)
        original_write = session_type.write_events_from_thread
        injected = False

        def commit_then_raise(self, events, **kwargs):
            nonlocal injected
            result = original_write(self, events, **kwargs)
            phase = (
                "start"
                if any(isinstance(event, ModelCallStartEvent) for event in events)
                else "terminal"
                if any(isinstance(event, ModelCallEndEvent) for event in events)
                else "semantic"
                if any(
                    getattr(event, "model_stream_attribution", None) is not None
                    for event in events
                )
                else "other"
            )
            if phase == failure_phase and not injected:
                injected = True
                raise asyncio.CancelledError(
                    f"synthetic acknowledgement loss after {phase} commit"
                )
            return result

        monkeypatch.setattr(
            session_type,
            "write_events_from_thread",
            commit_then_raise,
        )
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id=f"context:commit-{failure_phase}",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        completion = await handle.wait_completed()

        assert injected is True
        assert completion.terminal_outcome == "completed"
        events = tuple(session.event_log.iter())
        assert sum(isinstance(event, ModelCallStartEvent) for event in events) == 1
        assert sum(isinstance(event, ModelCallEndEvent) for event in events) == 1
        assert tuple(event.sequence for event in events) == tuple(
            range(1, len(events) + 1)
        )

    asyncio.run(scenario())


def test_model_terminal_precommit_failure_retries_same_provider_outcome(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.runtime.session import EventCommitError

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="provider completed"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        session_type = type(session)
        original_write = session_type.write_events_from_thread
        failed_once = False

        def fail_before_terminal_commit(self, events, **kwargs):
            nonlocal failed_once
            if not failed_once and any(
                isinstance(event, ModelCallEndEvent) for event in events
            ):
                failed_once = True
                raise EventCommitError("synthetic terminal pre-commit failure")
            return original_write(self, events, **kwargs)

        monkeypatch.setattr(
            session_type,
            "write_events_from_thread",
            fail_before_terminal_commit,
        )
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:stable-terminal-retry",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        completion = await handle.wait_completed()

        assert failed_once is True
        assert completion.terminal_outcome == "completed"
        ends = tuple(
            event
            for event in session.event_log.iter()
            if isinstance(event, ModelCallEndEvent)
        )
        assert len(ends) == 1
        assert ends[0].outcome == "completed"

    asyncio.run(scenario())


def test_naked_model_worker_cancellation_retains_physical_owner_until_read_exits(
    tmp_path,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class CancellationResistantTransport:
            api = "cancel-resistant"
            binding_id = "test.cancel-resistant"
            contract_version = "v1"

            async def stream(self, *, call, context, event_context):
                del call, context
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                async for event in MockTransport(text="late").stream(
                    call=None,
                    context=None,
                    event_context=event_context,
                ):
                    yield event

        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="cancel-resistant",
        )
        registry = LLMTransportRegistry()
        registry.register(CancellationResistantTransport())
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("wait"),),
                context_id="context:naked-cancel",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(
            runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert handle._task is not None
        handle._task.cancel()
        await asyncio.sleep(0.02)

        assert handle.completion.done() is False
        assert handle.has_physical_operations() is True
        assert session.model_stream_execution_registry.active_handle_count() == 1

        release.set()
        completion = await asyncio.wait_for(handle.wait_completed(), timeout=1)
        assert completion.terminal_outcome == "runtime_error"
        assert handle.has_physical_operations() is False

    asyncio.run(scenario())


def test_model_commit_unknown_keeps_stream_owner_and_blocks_close() -> None:
    import asyncio

    from pulsara_agent.llm.execution import (
        ModelStreamCompletion,
        ModelStreamExecutionDrainBlocked,
        ModelStreamExecutionRegistry,
    )

    async def scenario() -> None:
        registry = ModelStreamExecutionRegistry()

        async def blocked_worker(_handle):
            return ModelStreamCompletion(
                resolved_model_call_id="model_call:" + "a" * 32,
                terminal_outcome="reconciliation_blocked",
                committed_events=(),
                diagnostic_code="synthetic_commit_outcome_unknown",
            )

        handle = registry.install_and_start(
            handle_id="model_stream:blocked",
            run_id="run:blocked",
            resolved_model_call_id="model_call:" + "a" * 32,
            subscription_start_sequence=0,
            worker=blocked_worker,
        )
        completion = await handle.wait_completed()
        await asyncio.sleep(0)

        assert completion.terminal_outcome == "reconciliation_blocked"
        assert registry.active_handle_count() == 1
        with pytest.raises(
            ModelStreamExecutionDrainBlocked,
            match="blocks teardown",
        ):
            await registry.drain_run(
                "run:blocked",
                reason="host_teardown",
                deadline_monotonic=asyncio.get_running_loop().time() + 1.0,
            )
        assert registry.active_handle_count() == 1

    asyncio.run(scenario())


def test_semantic_partial_or_unknown_keeps_stream_owner_and_blocks_close(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from pulsara_agent.llm.execution import ModelStreamExecutionDrainBlocked

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="unknown semantic ack"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        session_type = type(session)
        original_write = session_type.write_events_from_thread
        original_confirm = session_type.confirm_and_handoff_event_batch
        injected = False

        def commit_then_raise(self, events, **kwargs):
            nonlocal injected
            result = original_write(self, events, **kwargs)
            is_semantic = any(
                getattr(event, "model_stream_attribution", None) is not None
                for event in events
            )
            if is_semantic and not injected:
                injected = True
                raise asyncio.CancelledError(
                    "synthetic semantic acknowledgement loss"
                )
            return result

        def unknown_confirmation(self, candidates, **kwargs):
            if any(
                getattr(event, "model_stream_attribution", None) is not None
                for event in candidates
            ):
                raise OSError("synthetic confirmation read unavailable")
            return original_confirm(self, candidates, **kwargs)

        monkeypatch.setattr(
            session_type,
            "write_events_from_thread",
            commit_then_raise,
        )
        monkeypatch.setattr(
            session_type,
            "confirm_and_handoff_event_batch",
            unknown_confirmation,
        )
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = bind_test_context(
            call,
            test_llm_context(
                messages=(LLMMessage.user("Say hi"),),
                context_id="context:semantic-unknown",
                model_call_index=1,
            ),
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
            run_execution_activation=make_test_run_execution_activation(),
        )
        completion = await handle.wait_completed()

        assert injected is True
        assert completion.terminal_outcome == "reconciliation_blocked"
        assert session.ledger_reconciliation_required is True
        with pytest.raises(OSError, match="confirmation read unavailable"):
            await handle.wait_result()
        assert session.model_stream_execution_registry.active_handle_count() == 1
        with pytest.raises(ModelStreamExecutionDrainBlocked):
            await session.model_stream_execution_registry.drain_all(
                deadline_monotonic=asyncio.get_running_loop().time() + 1.0
            )
        assert not any(
            isinstance(event, ModelCallEndEvent)
            for event in session.event_log.iter()
        )

    asyncio.run(scenario())


def test_start_stream_registers_handle_before_worker_enters_validation_or_transport(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import pulsara_agent.llm.runtime as runtime_module

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="must not dispatch"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.MEMORY_REFLECTION,
        )
        context = bind_test_context(
            call,
            test_llm_context(messages=(LLMMessage.user("invalid"),)),
        )
        validation_saw_registered_owner = False

        def reject_after_owner_install(*, call, context):
            del call, context
            nonlocal validation_saw_registered_owner
            validation_saw_registered_owner = (
                session.model_stream_execution_registry.active_handle_count() == 1
            )
            raise ValueError("synthetic pre-start validation rejection")

        monkeypatch.setattr(
            runtime_module,
            "validate_model_context_for_call",
            reject_after_owner_install,
        )
        handle = _start_test_stream(runtime,
            call=call,
            context=context,
            event_context=EVENT_CONTEXT,
            runtime_session=session,
        )
        completion = await handle.wait_completed()

        assert validation_saw_registered_owner is True
        assert completion.terminal_outcome == "rejected_before_start"
        assert completion.committed_events == ()
        with pytest.raises(
            ValueError,
            match="synthetic pre-start validation rejection",
        ):
            await handle.wait_result()
        assert not session.event_log.iter()
        assert session.model_stream_execution_registry.active_handle_count() == 0

    asyncio.run(scenario())


def test_model_stream_worker_task_start_failure_removes_prestart_owner_without_run_fact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import pulsara_agent.llm.execution as execution_module

    async def scenario() -> None:
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
        )
        registry = LLMTransportRegistry()
        registry.register(MockTransport(text="must not dispatch"))
        runtime = LLMRuntime(config=config, registry=registry)
        session = in_memory_runtime_session(tmp_path)
        target = runtime.resolve_target(role=ModelRole.FLASH)
        call = runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.MEMORY_REFLECTION,
        )
        context = bind_test_context(
            call,
            test_llm_context(messages=(LLMMessage.user("never starts"),)),
        )

        def fail_task_start(*_args, **_kwargs):
            raise RuntimeError("synthetic model worker task start failure")

        monkeypatch.setattr(execution_module.asyncio, "create_task", fail_task_start)
        with pytest.raises(
            RuntimeError,
            match="synthetic model worker task start failure",
        ):
            _start_test_stream(runtime,
                call=call,
                context=context,
                event_context=EVENT_CONTEXT,
                runtime_session=session,
            )

        assert session.model_stream_execution_registry.active_handle_count() == 0
        assert not session.event_log.iter()

    asyncio.run(scenario())


def test_openai_responses_payload_uses_internal_context() -> None:
    limits = test_model_limits(default_output_tokens=128)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        pro_limits=limits,
    )
    context = test_llm_context(
        system_prompt="You are Pulsara.",
        messages=(LLMMessage.user("Use the tool."),),
        tools=(
            ToolSpec(
                name="lookup",
                description="Look up a value.",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
        ),
    )

    payload = build_responses_payload(
        call=payload_call(
            config,
            options=LLMOptions(
                reasoning_effort="medium",
            ),
        ),
        context=context,
    )

    assert payload["model"] == "pro"
    assert payload["instructions"] == "You are Pulsara."
    assert payload["input"][0]["role"] == "user"
    assert payload["input"][0]["content"] == "Use the tool."
    assert payload["tools"][0]["name"] == "lookup"
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["max_output_tokens"] == 128


def test_openai_responses_payload_uses_function_call_output_items() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    context = test_llm_context(
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.tool_call(
                tool_call_id="call_responses_123",
                name="lookup",
                arguments='{"q":"pulsara"}',
            ),
            LLMMessage.tool_result("found", tool_call_id="call_responses_123"),
        )
    )

    payload = build_responses_payload(call=payload_call(config), context=context)

    assert payload["input"][0]["role"] == "user"
    assert payload["input"][1] == {
        "type": "function_call",
        "call_id": "call_responses_123",
        "name": "lookup",
        "arguments": '{"q":"pulsara"}',
    }
    assert payload["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_responses_123",
        "output": "found",
    }
    assert all(item.get("role") != "tool" for item in payload["input"])


def test_openai_responses_payload_preserves_message_level_system_items() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    context = test_llm_context(
        messages=(
            LLMMessage.user("original user input"),
            LLMMessage.system("Pulsara note: previous turn failed."),
            LLMMessage.user("please continue"),
        )
    )

    payload = build_responses_payload(call=payload_call(config), context=context)

    assert payload["input"][0] == {
        "role": "user",
        "content": "original user input",
    }
    assert payload["input"][1] == {
        "role": "system",
        "content": "Pulsara note: previous turn failed.",
    }
    assert payload["input"][2] == {
        "role": "user",
        "content": "please continue",
    }


def test_openai_responses_payload_keeps_current_user_after_prior_assistant_text() -> (
    None
):
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    context = test_llm_context(
        messages=(
            LLMMessage.user("hello"),
            LLMMessage.assistant("Hello! How can I help?"),
            LLMMessage.user("你能帮我把这个贪吃蛇小游戏做的再好一些吗？发挥你的能力"),
        )
    )

    payload = build_responses_payload(call=payload_call(config), context=context)

    assert payload["input"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {
            "role": "user",
            "content": "你能帮我把这个贪吃蛇小游戏做的再好一些吗？发挥你的能力",
        },
    ]


def test_openai_responses_payload_expands_assistant_turn_tool_calls() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    context = test_llm_context(
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.assistant_turn(
                text="I will call lookup.",
                thinking="private reasoning",
                tool_calls=(
                    LLMToolCall(
                        id="call_responses_123",
                        name="lookup",
                        arguments='{"q":"pulsara"}',
                    ),
                ),
            ),
            LLMMessage.tool_result("found", tool_call_id="call_responses_123"),
        )
    )

    payload = build_responses_payload(call=payload_call(config), context=context)

    assert payload["input"][1]["role"] == "assistant"
    assert payload["input"][1]["content"] == "I will call lookup."
    assert payload["input"][2] == {
        "type": "function_call",
        "call_id": "call_responses_123",
        "name": "lookup",
        "arguments": '{"q":"pulsara"}',
    }
    assert payload["input"][3] == {
        "type": "function_call_output",
        "call_id": "call_responses_123",
        "output": "found",
    }


def test_openai_responses_events_translate_to_agent_events() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    builder = transport_builder_for_test(config)

    text_events = translate_responses_event(
        {"type": "response.output_text.delta", "delta": "hello"},
        builder=builder,
    )
    start_events = translate_responses_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "name": "lookup",
            },
        },
        builder=builder,
    )
    args_events = translate_responses_event(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"q"',
        },
        builder=builder,
    )
    done_events = translate_responses_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "name": "lookup",
                "arguments": '{"q": "json-ld"}',
            },
        },
        builder=builder,
    )

    assert isinstance(text_events[0], TextBlockStartEvent)
    assert isinstance(text_events[1], TextBlockDeltaEvent)
    assert isinstance(start_events[0], ToolCallStartEvent)
    assert len(args_events) == 1
    assert isinstance(args_events[0], ToolCallDeltaEvent)
    assert args_events[0].tool_call_id == "fc_1"
    assert args_events[0].delta == '{"q"'
    assert len(done_events) == 1
    assert isinstance(done_events[0], ToolCallEndEvent)
    assert done_events[0].tool_call_id == "fc_1"


def test_openai_responses_transport_can_stream_mock_raw_events() -> None:
    import asyncio

    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        _mock_events=[
            {"type": "response.output_text.delta", "delta": "hi"},
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_1", "name": "lookup"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "delta": "{}",
            },
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "id": "fc_1", "name": "lookup"},
            },
        ],
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.PRO,
            test_llm_context(messages=(LLMMessage.user("hi"),)),
        )

    events = asyncio.run(collect())

    assert not any(isinstance(event, ModelCallStartEvent) for event in events)
    assert any(
        isinstance(event, TextBlockDeltaEvent) and event.delta == "hi"
        for event in events
    )
    assert any(
        isinstance(event, ToolCallStartEvent) and event.tool_call_name == "lookup"
        for event in events
    )
    assert any(
        isinstance(event, ToolCallDeltaEvent) and event.delta == "{}"
        for event in events
    )
    assert not any(isinstance(event, ModelCallEndEvent) for event in events)


def test_non_streaming_response_synthesizes_same_event_shape() -> None:
    builder = transport_builder_for_test()
    events = response_to_agent_events(
        response={
            "status": "completed",
            "reasoning": {"summary": [{"text": "brief thinking"}]},
            "output_text": "done",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "name": "lookup",
                    "arguments": '{"q": "pulsara"}',
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        },
        builder=builder,
    )

    assert isinstance(events[0], ThinkingBlockStartEvent)
    assert isinstance(events[1], ThinkingBlockDeltaEvent)
    assert isinstance(events[2], TextBlockStartEvent)
    assert isinstance(events[3], TextBlockDeltaEvent)
    assert isinstance(events[4], ToolCallStartEvent)
    assert isinstance(events[5], ToolCallDeltaEvent)
    assert isinstance(events[6], ToolCallEndEvent)
    assert any(isinstance(event, TextBlockEndEvent) for event in events)
    assert any(isinstance(event, ThinkingBlockEndEvent) for event in events)
    assert isinstance(events[-1], TransportUsageReport)
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 3
    assert events[-1].usage.output_tokens == 5
    assert events[-1].usage.total_tokens == 8


def test_reported_response_model_alias_is_accepted_by_default() -> None:
    import asyncio

    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="expected-model",
        flash_model="flash",
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        _mock_events=[
            {
                "type": "response.completed",
                "response": {"model": "fallback-model", "usage": None},
            }
        ],
    )

    events = asyncio.run(
        collect_transport_events(
            transport,
            config,
            ModelRole.PRO,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )
    )

    report = next(event for event in events if isinstance(event, TransportUsageReport))
    assert report.reported_model_id == "fallback-model"


def test_reported_chat_model_alias_is_accepted_by_default() -> None:
    import asyncio

    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="model-alias",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        _mock_chunks=[{"model": "provider-snapshot", "choices": []}],
    )

    events = asyncio.run(
        collect_transport_events(
            transport,
            config,
            ModelRole.PRO,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )
    )

    report = next(event for event in events if isinstance(event, TransportUsageReport))
    assert report.reported_model_id == "provider-snapshot"


def test_exact_model_identity_policy_rejects_mismatch() -> None:
    import asyncio

    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="expected-model",
        flash_model="flash",
        provider_profile=ProviderProfile(
            model_identity_policy=ModelIdentityPolicy.EXACT
        ),
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        _mock_events=[
            {
                "type": "response.completed",
                "response": {"model": "fallback-model", "usage": None},
            }
        ],
    )

    with pytest.raises(
        LLMTransportContractError, match="transport_changed_model_target"
    ):
        asyncio.run(
            collect_transport_events(
                transport,
                config,
                ModelRole.PRO,
                test_llm_context(messages=(LLMMessage.user("ping"),)),
            )
        )


def test_reported_model_identity_changes_within_attempt_is_rejected() -> None:
    import asyncio

    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="model-alias",
        flash_model="flash",
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        _mock_events=[
            {"type": "response.created", "response": {"model": "snapshot-a"}},
            {
                "type": "response.completed",
                "response": {"model": "snapshot-b", "usage": None},
            },
        ],
    )

    with pytest.raises(
        LLMTransportContractError, match="identity changed within stream"
    ):
        asyncio.run(
            collect_transport_events(
                transport,
                config,
                ModelRole.PRO,
                test_llm_context(messages=(LLMMessage.user("ping"),)),
            )
        )


def test_missing_response_model_is_allowed_but_not_confirmation() -> None:
    import asyncio

    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="expected-model",
        flash_model="flash",
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        _mock_events=[{"type": "response.completed", "response": {"usage": None}}],
    )

    events = asyncio.run(
        collect_transport_events(
            transport,
            config,
            ModelRole.PRO,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], TransportUsageReport)
    assert events[0].usage_status == "missing"
    assert events[0].reported_model_id is None


def test_openai_responses_tool_calls_prefer_call_id_over_item_id() -> None:
    builder = transport_builder_for_test()

    events = response_to_agent_events(
        response={
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "call_responses_1",
                    "name": "lookup",
                    "arguments": '{"q":"pulsara"}',
                }
            ],
        },
        builder=builder,
    )

    start = next(event for event in events if isinstance(event, ToolCallStartEvent))
    delta = next(event for event in events if isinstance(event, ToolCallDeltaEvent))
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert start.tool_call_id == "call_responses_1"
    assert delta.tool_call_id == "call_responses_1"
    assert end.tool_call_id == "call_responses_1"


def test_openai_responses_streaming_arguments_map_item_id_to_call_id() -> None:
    builder = transport_builder_for_test()

    events = []
    events.extend(
        translate_responses_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "call_responses_1",
                    "name": "lookup",
                },
            },
            builder=builder,
        )
    )
    events.extend(
        translate_responses_event(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_item_1",
                "delta": '{"q":"pulsara"}',
            },
            builder=builder,
        )
    )
    events.extend(
        translate_responses_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "call_responses_1",
                    "name": "lookup",
                },
            },
            builder=builder,
        )
    )

    assert isinstance(events[0], ToolCallStartEvent)
    assert isinstance(events[1], ToolCallDeltaEvent)
    assert isinstance(events[2], ToolCallEndEvent)
    assert events[0].tool_call_id == "call_responses_1"
    assert events[1].tool_call_id == "call_responses_1"
    assert events[2].tool_call_id == "call_responses_1"


def test_openai_responses_error_event_emits_run_error_without_model_end() -> None:
    builder = transport_builder_for_test()

    events = translate_responses_event(
        {"type": "error", "message": "provider exploded", "code": "bad_request"},
        builder=builder,
    )

    assert len(events) == 1
    assert isinstance(events[0], RunErrorEvent)
    assert events[0].message == "provider exploded"
    assert events[0].code == "openai_responses_error"


def test_openai_responses_transport_uses_sdk_stream() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        responses_events=[
            {"type": "response.output_text.delta", "delta": "pong"},
            {
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
                },
            },
        ]
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test", timeout_seconds=7, _client=fake_client
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    assert fake_client.responses.calls[0]["model"] == "flash"
    assert fake_client.responses.calls[0]["stream"] is True
    assert isinstance(events[0], TextBlockStartEvent)
    assert events[1].delta == "pong"
    assert isinstance(events[-1], TransportUsageReport)
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 1
    assert events[-1].usage.output_tokens == 2
    assert events[-1].usage.total_tokens == 3


def test_openai_responses_transport_emits_run_error_event() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(responses_error=RuntimeError("boom"))
    transport = OpenAIResponsesTransport(api_key="sk-test", _client=fake_client)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.PRO,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    assert len(events) == 1
    assert isinstance(events[0], RunErrorEvent)
    assert events[0].type is EventType.RUN_ERROR
    assert events[0].message == "boom"
    assert events[0].code == "openai_responses_error"
    assert events[0].metadata["provider_data"]["type"] == "RuntimeError"
    assert events[0].metadata["provider_data"]["message"] == "boom"
    assert "RuntimeError" in events[0].metadata["provider_data"]["repr"]


def test_openai_responses_transport_retries_pre_output_failure() -> None:
    import asyncio

    try:
        try:
            raise OSError("socket reset by peer")
        except OSError as exc:
            raise RuntimeError("Connection error.") from exc
    except RuntimeError as exc:
        connection_error = exc
    fake_client = FakeOpenAIClient(
        responses_script=[
            connection_error,
            [
                {"type": "response.output_text.delta", "delta": "pong"},
                {"type": "response.completed", "response": {}},
            ],
        ]
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    assert len(fake_client.responses.calls) == 2
    assert fake_client.close_count == 0
    retry_events = [event for event in events if isinstance(event, CustomEvent)]
    assert len(retry_events) == 1
    assert retry_events[0].name == "llm.retry"
    assert retry_events[0].value["attempt"] == 1
    assert retry_events[0].value["reason"] == "transport_error"
    assert [type(event) for event in events].count(ModelCallStartEvent) == 0
    assert any(
        isinstance(event, TextBlockDeltaEvent) and event.delta == "pong"
        for event in events
    )
    assert isinstance(events[-1], TransportUsageReport)


def test_network_retry_discards_abandoned_attempt_reported_identity() -> None:
    import asyncio

    try:
        try:
            raise OSError("socket reset by peer")
        except OSError as exc:
            raise RuntimeError("Connection error.") from exc
    except RuntimeError as exc:
        connection_error = exc
    fake_client = FakeOpenAIClient(
        responses_script=[
            [
                {"type": "response.created", "response": {"model": "snapshot-a"}},
                connection_error,
            ],
            [
                {"type": "response.created", "response": {"model": "snapshot-b"}},
                {
                    "type": "response.completed",
                    "response": {"model": "snapshot-b", "usage": None},
                },
            ],
        ]
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="model-alias",
        flash_model="flash",
    )

    events = asyncio.run(
        collect_transport_events(
            transport,
            config,
            ModelRole.PRO,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )
    )

    report = next(event for event in events if isinstance(event, TransportUsageReport))
    assert report.reported_model_id == "snapshot-b"


def test_openai_responses_transport_owned_client_closes_once_after_retry(
    monkeypatch,
) -> None:
    import asyncio
    from pulsara_agent.llm.adapters.openai import responses as responses_module

    fake_client = FakeOpenAIClient(
        responses_script=[
            ConnectionError("reset one"),
            [{"type": "response.completed", "response": {}}],
        ]
    )
    builder_calls = []

    def fake_build_async_openai_client(**kwargs):
        builder_calls.append(kwargs)
        return fake_client

    monkeypatch.setattr(
        responses_module, "build_async_openai_client", fake_build_async_openai_client
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    asyncio.run(collect())

    assert len(builder_calls) == 1
    assert builder_calls[0]["max_retries"] == 0
    assert len(fake_client.responses.calls) == 2
    assert fake_client.close_count == 1


def test_openai_responses_transport_does_not_retry_after_text_delta() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        responses_script=[
            [
                {"type": "response.output_text.delta", "delta": "partial"},
                RuntimeError("stream broke after text"),
            ],
        ]
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=3, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    assert len(fake_client.responses.calls) == 1
    assert not any(
        isinstance(event, CustomEvent) and event.name == "llm.retry" for event in events
    )
    error = next(event for event in events if isinstance(event, RunErrorEvent))
    assert (
        error.metadata["provider_data"]["retry"]["skipped_reason"]
        == "semantic_output_started"
    )
    assert any(isinstance(event, TextBlockEndEvent) for event in events)


def test_openai_responses_transport_retry_exhausted_has_trace() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        responses_script=[
            ConnectionError("reset one"),
            ConnectionError("reset two"),
        ]
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    retry_events = [event for event in events if isinstance(event, CustomEvent)]
    assert len(retry_events) == 1
    error = next(event for event in events if isinstance(event, RunErrorEvent))
    retry = error.metadata["provider_data"]["retry"]
    assert retry["exhausted"] is True
    assert retry["attempts"] == 2
    assert retry["traces"][0]["error_message"] == "reset one"


def test_model_stream_retry_remains_adapter_private_and_reuses_call() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        responses_script=[
            ConnectionError("reset one"),
            [
                {"type": "response.output_text.delta", "delta": "ok"},
                {"type": "response.completed", "response": {}},
            ],
        ]
    )
    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    runtime = LLMRuntime(config=config, registry=registry)

    events = asyncio.run(
        collect_events(
            runtime,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )
    )

    assert isinstance(events[0], ReplyStartEvent)
    assert isinstance(events[1], RolloutBudgetReservationCreatedEvent)
    assert isinstance(events[2], ModelCallStartEvent)
    assert not any(
        isinstance(event, CustomEvent) and event.name == "llm.retry"
        for event in events
    )
    assert any(
        isinstance(event, TextBlockDeltaEvent) and event.delta == "ok"
        for event in events
    )
    starts = [event for event in events if isinstance(event, ModelCallStartEvent)]
    ends = [event for event in events if isinstance(event, ModelCallEndEvent)]
    assert len(starts) == len(ends) == 1
    assert (
        ends[0].resolved_model_call_id == starts[0].resolved_call.resolved_model_call_id
    )
    assert len(fake_client.responses.calls) == 2
    assert fake_client.responses.calls[0] == fake_client.responses.calls[1]
    assert isinstance(events[-1], ReplyEndEvent)
def test_provider_error_data_includes_exception_chain() -> None:
    try:
        try:
            raise OSError("socket reset by peer")
        except OSError as exc:
            raise RuntimeError("Connection error.") from exc
    except RuntimeError as exc:
        data = provider_error_data(exc)

    assert data["type"] == "RuntimeError"
    assert data["message"] == "Connection error."
    assert "RuntimeError" in data["repr"]
    assert data["causes"][0]["relation"] == "cause"
    assert data["causes"][0]["type"] == "OSError"
    assert data["causes"][0]["message"] == "socket reset by peer"


def test_openai_chat_completions_transport_error_metadata_includes_cause() -> None:
    import asyncio

    try:
        try:
            raise OSError("deepseek stream closed")
        except OSError as exc:
            raise RuntimeError("Connection error.") from exc
    except RuntimeError as exc:
        fake_client = FakeOpenAIClient(chat_error=exc)
    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(enabled=False),
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())
    provider_data = events[0].metadata["provider_data"]

    assert isinstance(events[0], RunErrorEvent)
    assert events[0].message == "Connection error."
    assert events[0].code == "openai_chat_completions_error"
    assert provider_data["type"] == "RuntimeError"
    assert provider_data["causes"][0]["type"] == "OSError"
    assert provider_data["causes"][0]["message"] == "deepseek stream closed"


def test_openai_chat_completions_payload_uses_internal_context() -> None:
    limits = test_model_limits(default_output_tokens=64)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
        pro_limits=limits,
    )
    context = test_llm_context(
        system_prompt="You are Pulsara.",
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.tool_call(
                tool_call_id="call_chat_123",
                name="lookup",
                arguments='{"q":"pulsara"}',
            ),
            LLMMessage.tool_result("found", tool_call_id="call_chat_123"),
        ),
        tools=(
            ToolSpec(
                name="lookup",
                description="Look up a value.",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
        ),
    )

    payload = build_chat_completions_payload(
        call=payload_call(
            config,
            options=LLMOptions(reasoning_effort="medium"),
            api="chat",
        ),
        context=context,
    )

    assert payload["model"] == "pro"
    assert payload["messages"][0] == {"role": "system", "content": "You are Pulsara."}
    assert payload["messages"][1] == {"role": "user", "content": "Use lookup."}
    assert payload["messages"][2]["role"] == "assistant"
    assert payload["messages"][2]["tool_calls"][0]["id"] == "call_chat_123"
    assert payload["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_chat_123",
        "content": "found",
    }
    assert payload["tools"][0]["function"]["name"] == "lookup"
    assert payload["max_completion_tokens"] == 64
    assert payload["reasoning_effort"] == "medium"
    assert payload["stream_options"] == {"include_usage": True}


def test_openai_chat_completions_lowers_runtime_observation_with_frozen_carrier() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    call = payload_call(config, options=LLMOptions(), api="chat")
    carrier = call.target.fact.runtime_observation_carrier
    assert carrier is not None
    assert carrier.carrier_id == "pulsara.runtime_observation.system_message"
    context = bind_test_context(
        call,
        test_llm_context(
            messages=(
                LLMMessage.user("continue"),
                LLMMessage.runtime_observation("rollout_phase=finalization_only"),
            )
        ),
    )

    payload = build_chat_completions_payload(call=call, context=context)

    assert payload["messages"][-1] == {
        "role": "system",
        "content": "rollout_phase=finalization_only",
    }


def test_openai_chat_completions_payload_groups_adjacent_tool_calls() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    context = test_llm_context(
        messages=(
            LLMMessage.user("Use both tools."),
            LLMMessage.tool_call(
                tool_call_id="call_1",
                name="first_tool",
                arguments='{"a":1}',
            ),
            LLMMessage.tool_call(
                tool_call_id="call_2",
                name="second_tool",
                arguments='{"b":2}',
            ),
            LLMMessage.tool_result("first result", tool_call_id="call_1"),
            LLMMessage.tool_result("second result", tool_call_id="call_2"),
        )
    )

    payload = build_chat_completions_payload(
        call=payload_call(config, api="chat"),
        context=context,
    )

    assert payload["messages"][0] == {"role": "user", "content": "Use both tools."}
    assert payload["messages"][1]["role"] == "assistant"
    assert [call["id"] for call in payload["messages"][1]["tool_calls"]] == [
        "call_1",
        "call_2",
    ]
    assert payload["messages"][2]["role"] == "tool"
    assert payload["messages"][2]["tool_call_id"] == "call_1"
    assert payload["messages"][3]["role"] == "tool"
    assert payload["messages"][3]["tool_call_id"] == "call_2"


def test_openai_chat_completions_payload_preserves_message_level_system_items() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    context = test_llm_context(
        messages=(
            LLMMessage.user("original user input"),
            LLMMessage.system("Pulsara note: previous turn failed."),
            LLMMessage.user("please continue"),
        )
    )

    payload = build_chat_completions_payload(
        call=payload_call(config, api="chat"),
        context=context,
    )

    assert payload["messages"][0] == {"role": "user", "content": "original user input"}
    assert payload["messages"][1] == {
        "role": "system",
        "content": "Pulsara note: previous turn failed.",
    }
    assert payload["messages"][2] == {"role": "user", "content": "please continue"}


def test_openai_chat_completions_payload_replays_assistant_thinking_with_tool_calls() -> (
    None
):
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
        provider_profile=ProviderProfile(
            wire_api=OPENAI_CHAT_COMPLETIONS_API,
            request_extra_body={"thinking": {"type": "enabled"}},
            omit_params_when_thinking=("temperature",),
            thinking=ThinkingProfile(
                enabled=True,
                replay_policy=ThinkingReplayPolicy.WHEN_TOOL_CALLS,
            ),
        ),
    )
    context = test_llm_context(
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.assistant_turn(
                text="I will look that up.",
                thinking="Need a tool result before answering.",
                tool_calls=(
                    LLMToolCall(
                        id="call_1", name="lookup", arguments='{"q":"pulsara"}'
                    ),
                ),
            ),
            LLMMessage.tool_result("found", tool_call_id="call_1"),
        )
    )

    payload = build_chat_completions_payload(
        call=payload_call(
            config,
            options=LLMOptions(reasoning_effort="medium"),
            api="chat",
        ),
        context=context,
    )

    assistant = payload["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "I will look that up."
    assert assistant["reasoning_content"] == "Need a tool result before answering."
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert payload["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in payload
    assert payload["reasoning_effort"] == "medium"


def test_openai_chat_completions_payload_does_not_replay_thinking_by_default() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    context = test_llm_context(
        messages=(
            LLMMessage.assistant_turn(
                text="I will look that up.",
                thinking="Provider-private reasoning.",
                tool_calls=(LLMToolCall(id="call_1", name="lookup", arguments="{}"),),
            ),
        )
    )

    payload = build_chat_completions_payload(
        call=payload_call(config, api="chat"),
        context=context,
    )

    assert "reasoning_content" not in payload["messages"][0]


def test_openai_chat_completions_transport_can_stream_mock_chunks() -> None:
    import asyncio

    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        _mock_chunks=[
            {"choices": [{"delta": {"content": "hi"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_chat_1",
                                    "function": {"name": "lookup", "arguments": '{"q"'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ':"pulsara"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 4,
                    "total_tokens": 6,
                },
            },
        ],
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.PRO,
            test_llm_context(messages=(LLMMessage.user("hi"),)),
        )

    events = asyncio.run(collect())

    assert not any(isinstance(event, ModelCallStartEvent) for event in events)
    assert any(
        isinstance(event, TextBlockDeltaEvent) and event.delta == "hi"
        for event in events
    )
    assert any(
        isinstance(event, ToolCallStartEvent) and event.tool_call_id == "call_chat_1"
        for event in events
    )
    assert [
        event.delta for event in events if isinstance(event, ToolCallDeltaEvent)
    ] == [
        '{"q"',
        ':"pulsara"}',
    ]
    assert any(
        isinstance(event, ToolCallEndEvent) and event.tool_call_id == "call_chat_1"
        for event in events
    )
    assert isinstance(events[-1], TransportUsageReport)
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 2
    assert events[-1].usage.output_tokens == 4
    assert events[-1].usage.total_tokens == 6


def test_openai_chat_completions_translates_reasoning_content_delta() -> None:
    builder = transport_builder_for_test()
    accumulator = ChatToolCallAccumulator(builder=builder)

    events = translate_chat_completion_chunk(
        {"choices": [{"delta": {"reasoning_content": "think", "content": "answer"}}]},
        builder=builder,
        accumulator=accumulator,
    )

    assert isinstance(events[0], ThinkingBlockStartEvent)
    assert isinstance(events[1], ThinkingBlockDeltaEvent)
    assert events[1].delta == "think"
    assert isinstance(events[2], TextBlockStartEvent)
    assert isinstance(events[3], TextBlockDeltaEvent)
    assert events[3].delta == "answer"


def test_openai_chat_completions_transport_uses_sdk_stream() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        chat_chunks=[
            {"choices": [{"delta": {"content": "pong"}}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                },
            },
        ]
    )
    transport = OpenAIChatCompletionsTransport(api_key="sk-test", _client=fake_client)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    assert fake_client.chat.completions.calls[0]["model"] == "flash"
    assert fake_client.chat.completions.calls[0]["stream"] is True
    assert isinstance(events[0], TextBlockStartEvent)
    assert events[1].delta == "pong"
    assert isinstance(events[-1], TransportUsageReport)
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 1
    assert events[-1].usage.output_tokens == 2
    assert events[-1].usage.total_tokens == 3


def test_openai_chat_completions_transport_retries_pre_output_failure() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        chat_script=[
            ConnectionError("stream create reset"),
            [
                {"choices": [{"delta": {"content": "pong"}}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 3,
                    },
                },
            ],
        ]
    )
    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    assert len(fake_client.chat.completions.calls) == 2
    assert fake_client.close_count == 0
    assert [type(event) for event in events].count(ModelCallStartEvent) == 0
    retry_events = [event for event in events if isinstance(event, CustomEvent)]
    assert len(retry_events) == 1
    assert retry_events[0].value["reason"] == "transport_error"
    assert any(
        isinstance(event, TextBlockDeltaEvent) and event.delta == "pong"
        for event in events
    )
    assert isinstance(events[-1], TransportUsageReport)


def test_openai_chat_completions_owned_client_closes_once_after_retry(
    monkeypatch,
) -> None:
    import asyncio
    from pulsara_agent.llm.adapters.openai import chat_completions as chat_module

    fake_client = FakeOpenAIClient(
        chat_script=[
            ConnectionError("reset one"),
            [
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 0,
                        "total_tokens": 1,
                    },
                }
            ],
        ]
    )
    builder_calls = []

    def fake_build_async_openai_client(**kwargs):
        builder_calls.append(kwargs)
        return fake_client

    monkeypatch.setattr(
        chat_module, "build_async_openai_client", fake_build_async_openai_client
    )
    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    asyncio.run(collect())

    assert len(builder_calls) == 1
    assert builder_calls[0]["max_retries"] == 0
    assert len(fake_client.chat.completions.calls) == 2
    assert fake_client.close_count == 1


def test_openai_chat_completions_transport_does_not_retry_after_tool_delta() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        chat_script=[
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_chat_1",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"q"',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                ConnectionError("stream broke after tool delta"),
            ],
        ]
    )
    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=3, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    assert len(fake_client.chat.completions.calls) == 1
    assert not any(
        isinstance(event, CustomEvent) and event.name == "llm.retry" for event in events
    )
    assert any(
        isinstance(event, ToolCallDeltaEvent) and event.delta == '{"q"'
        for event in events
    )
    assert any(isinstance(event, ToolCallEndEvent) for event in events)
    error = next(event for event in events if isinstance(event, RunErrorEvent))
    assert (
        error.metadata["provider_data"]["retry"]["skipped_reason"]
        == "semantic_output_started"
    )


def test_openai_chat_completions_retry_exhausted_has_trace() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        chat_script=[
            ConnectionError("reset one"),
            ConnectionError("reset two"),
        ]
    )
    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        retry_config=LLMRetryConfig(
            attempts=2, base_delay_seconds=0.01, jitter_ratio=0
        ),
        retry_sleep=no_retry_sleep,
        _client=fake_client,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return await collect_transport_events(
            transport,
            config,
            ModelRole.FLASH,
            test_llm_context(messages=(LLMMessage.user("ping"),)),
        )

    events = asyncio.run(collect())

    retry_events = [event for event in events if isinstance(event, CustomEvent)]
    assert len(retry_events) == 1
    error = next(event for event in events if isinstance(event, RunErrorEvent))
    retry = error.metadata["provider_data"]["retry"]
    assert retry["exhausted"] is True
    assert retry["attempts"] == 2
    assert retry["traces"][0]["error_message"] == "reset one"


def test_openai_chat_completions_caches_arguments_until_tool_call_id_arrives() -> None:
    builder = transport_builder_for_test()
    accumulator = ChatToolCallAccumulator(builder=builder)

    first_events = translate_chat_completion_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"q"'}}]
                    }
                }
            ]
        },
        builder=builder,
        accumulator=accumulator,
    )
    second_events = translate_chat_completion_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_late",
                                "function": {"name": "lookup"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        builder=builder,
        accumulator=accumulator,
    )

    assert first_events == []
    assert isinstance(second_events[0], ToolCallStartEvent)
    assert second_events[0].tool_call_id == "call_late"
    assert isinstance(second_events[1], ToolCallDeltaEvent)
    assert second_events[1].tool_call_id == "call_late"
    assert second_events[1].delta == '{"q"'
    assert isinstance(second_events[2], ToolCallEndEvent)


def test_default_llm_runtime_registers_openai_responses_transport() -> None:
    retry_config = LLMRetryConfig(attempts=4, base_delay_seconds=0.1, jitter_ratio=0)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        retry=retry_config,
        openai_sdk_max_retries=0,
    )

    runtime = build_llm_runtime(config)

    responses_transport = runtime._registry.get(OPENAI_RESPONSES_API)
    chat_transport = runtime._registry.get(OPENAI_CHAT_COMPLETIONS_API)
    assert isinstance(responses_transport, SanitizingLLMTransport)
    assert isinstance(chat_transport, SanitizingLLMTransport)
    assert isinstance(responses_transport._raw_transport, OpenAIResponsesTransport)
    assert isinstance(chat_transport._raw_transport, OpenAIChatCompletionsTransport)
    assert responses_transport._raw_transport.retry_config is retry_config
    assert chat_transport._raw_transport.retry_config is retry_config
    assert responses_transport._raw_transport.openai_sdk_max_retries == 0
    assert chat_transport._raw_transport.openai_sdk_max_retries == 0


def transport_builder_for_test(config: LLMConfig | None = None):
    config = config or test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    return AgentEventBuilder(
        event_context=EVENT_CONTEXT,
    )


class FakeAsyncStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        event = self._events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


class FakeResponsesEndpoint:
    def __init__(self, *, events=None, error=None, script=None):
        self.events = events or []
        self.error = error
        self.script = list(script) if script is not None else None
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.script is not None:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return FakeAsyncStream(item)
        if self.error is not None:
            raise self.error
        return FakeAsyncStream(self.events)


class FakeOpenAIClient:
    def __init__(
        self,
        *,
        responses_events=None,
        responses_error=None,
        responses_script=None,
        chat_chunks=None,
        chat_error=None,
        chat_script=None,
    ):
        self.responses = FakeResponsesEndpoint(
            events=responses_events,
            error=responses_error,
            script=responses_script,
        )
        self.chat = FakeChatNamespace(
            chunks=chat_chunks, error=chat_error, script=chat_script
        )
        self.close_count = 0

    async def close(self):
        self.close_count += 1


class FakeChatCompletionsEndpoint:
    def __init__(self, *, chunks=None, error=None, script=None):
        self.chunks = chunks or []
        self.error = error
        self.script = list(script) if script is not None else None
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.script is not None:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return FakeAsyncStream(item)
        if self.error is not None:
            raise self.error
        return FakeAsyncStream(self.chunks)


class FakeChatNamespace:
    def __init__(self, *, chunks=None, error=None, script=None):
        self.completions = FakeChatCompletionsEndpoint(
            chunks=chunks, error=error, script=script
        )
