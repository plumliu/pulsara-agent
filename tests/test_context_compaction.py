from tests.support.runtime_owner import build_test_agent_runtime


import asyncio
from dataclasses import replace
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.conftest import (
    open_test_root_rollout_run,
    run_end_contract_fields,
    run_start_permission_fields,
    tool_result_end_contract_fields,
)

from tests.support.raw_provider import (
    RawProviderTextBlockEnd,
    RawProviderTextBlockStart,
    RawProviderTextDelta,
    RawProviderToolCallDelta,
    RawProviderToolCallEnd,
    RawProviderToolCallStart,
)

from tests.support.model_stream import (
    make_text_block_end_event,
    make_text_block_segment_event,
    make_text_block_start_event,
    make_thinking_block_end_event,
    make_thinking_block_segment_event,
    make_thinking_block_start_event,
    make_tool_call_arguments_segment_event,
    make_tool_call_end_event,
    make_tool_call_start_event,
)

from pulsara_agent.event import (
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    EventContext,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog, dump_agent_event, load_agent_event
from pulsara_agent.host import HostSession, HostWorkspaceInput, resolve_workspace
from pulsara_agent.ports.run_execution import RunSuspendedOutcome
from pulsara_agent.host.transcript import rebuild_prior_messages
from pulsara_agent.llm import LLMRuntime, ModelRole
from tests.support import (
    bind_test_context,
    bind_test_provider_input_context,
    compaction_completed_contract_fields,
    compaction_failed_contract_fields,
    compaction_started_contract_fields,
    context_compiled_contract_fields,
    make_test_run_execution_activation,
    test_llm_config,
    test_llm_context,
    test_model_limits,
    test_model_slot,
)
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from tests.support.model_call import model_call_end_fields, model_call_start_fields
from tests.support.event_write import committed_event_write_result_fixture
from tests.support.host import component_test_host_core
from tests.support.mcp import (
    queue_ready_test_mcp_candidate,
    make_mcp_client_input_required,
)
from tests.support.settings import compatibility_storage_config
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.errors import (
    CompactionSummarizerInputBudgetExceeded,
    CompactionTargetUnreachable,
    ModelOptionUnsupported,
    ModelTargetBindingMismatch,
)
from pulsara_agent.llm.input import LLMMessage, MessageRole
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.raw_provider import RawProviderFailure
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.compaction.extension import (
    MemoryCompactionPostCompletionExtension,
)
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.message import (
    AssistantMsg,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.runtime.approval import (
    ApprovalResolution,
    ToolApprovalDecision,
)
from pulsara_agent.runtime.compaction.planner import strip_compaction_analysis
from pulsara_agent.runtime.compaction.inline import RuntimeContextCompactor
from pulsara_agent.runtime.compaction.commit import (
    CompactionCommitPendingAfterCancellation,
    RuntimeSessionCompactionEventCommitPort,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_runtime_event_deadline_budget,
)
from pulsara_agent.runtime.session import EventWriteResult
from tests.support.events import typed_non_transcript_event
from tests.support.runtime_session import in_memory_runtime_session
from pulsara_agent.runtime.compaction.service import (
    ContextCompactionAttemptResult,
    ContextCompactionInvocationFailed,
    ContextCompactionPolicy,
    ContextCompactionService,
    build_compaction_input,
    build_metadata_only_compaction_input,
    production_compaction_prompt,
)
from pulsara_agent.blocking_executor import auxiliary_io_executor
import pulsara_agent.runtime.compaction.service as compaction_service_module
from pulsara_agent.runtime.plan import (
    McpInputRequiredInteractionResolution,
    PendingMcpInputRequired,
    PendingPlanInteraction,
    PlanQuestionResolution,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.state import (
    RunActivationWorkingState,
    LoopTransition,
)
from pulsara_agent.runtime.transcript import (
    rebuild_prior_messages_before_sequence,
    rebuild_prior_messages_bounded,
)
from pulsara_agent.runtime.wiring import (
    AgentRuntimeWiring,
)
from tests.support.runtime_factory import (
    build_component_runtime_wiring,
)
from pulsara_agent.primitives.model_call import (
    CompactionObservedAfterMeasurementFact,
    CompactionTargetEstimateFact,
    ModelCallControlDisposition,
    ModelCallPurpose,
    RunTerminationIntentAttributionFact,
    sha256_fingerprint,
)
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import (
    McpServerConfig,
    McpStdioConfig,
)
from pulsara_agent.settings import PulsaraSettings
from tests.support.model_call import test_resolved_target_fact


class CompactScriptedTransport:
    api = "compact_scripted"
    binding_id = "test.compact_scripted"
    contract_version = "v1"

    def __init__(self, text: str) -> None:
        self.text = text
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator:
        self.contexts.append(context)
        yield RawProviderTextBlockStart(
            **event_context.event_fields(), block_id="text:compact"
        )
        yield RawProviderTextDelta(
            **event_context.event_fields(), block_id="text:compact", delta=self.text
        )
        yield RawProviderTextBlockEnd(
            **event_context.event_fields(), block_id="text:compact"
        )
        yield TransportUsageReport(
            usage_status="missing",
            usage=None,
            reported_model_id=call.target.fact.model_id,
        )


class HostInteractionTransport:
    api = "compact_scripted"
    binding_id = "test.host_interaction"
    contract_version = "v1"

    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator:
        self.contexts.append(context)
        reply = self.replies.pop(0)
        text = reply.get("text")
        if isinstance(text, str):
            block_id = f"text:host:{len(self.contexts)}"
            yield RawProviderTextBlockStart(
                **event_context.event_fields(), block_id=block_id
            )
            yield RawProviderTextDelta(
                **event_context.event_fields(), block_id=block_id, delta=text
            )
            yield RawProviderTextBlockEnd(
                **event_context.event_fields(), block_id=block_id
            )
        for item in reply.get("tool_calls", ()):  # type: ignore[union-attr]
            tool_call = dict(item)
            tool_call_id = str(tool_call["id"])
            tool_name = str(tool_call["name"])
            yield RawProviderToolCallStart(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
            )
            yield RawProviderToolCallDelta(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
                delta=str(tool_call["arguments"]),
            )
            yield RawProviderToolCallEnd(
                **event_context.event_fields(), tool_call_id=tool_call_id
            )
        yield TransportUsageReport(
            usage_status="missing",
            usage=None,
            reported_model_id=call.target.fact.model_id,
        )


class CompactErrorAfterTextTransport(CompactScriptedTransport):
    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator:
        del call
        self.contexts.append(context)
        yield RawProviderTextBlockStart(
            **event_context.event_fields(), block_id="text:compact"
        )
        yield RawProviderTextDelta(
            **event_context.event_fields(), block_id="text:compact", delta=self.text
        )
        yield RawProviderFailure(
            message="provider failed mid-summary",
            code_hint="provider_error",
        )


class BlockingCompactTransport(CompactScriptedTransport):
    def __init__(self) -> None:
        super().__init__("")
        self.entered = asyncio.Event()

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator:
        del call, context, event_context
        self.entered.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - keeps this an async generator
            yield None


def _target(service: ContextCompactionService):
    return service.llm_runtime.resolve_target(role=ModelRole.PRO)


def _should_auto_compact(service: ContextCompactionService, **kwargs) -> bool:
    return service.should_auto_compact(
        target_model_target=_target(service),
        **kwargs,
    )


async def _compact_if_needed(service: ContextCompactionService, **kwargs) -> bool:
    return bool(
        await service.compact_if_needed(
            target_model_target=_target(service),
            **kwargs,
        )
    )


async def _compact(service: ContextCompactionService, **kwargs):
    result = await _compact_attempt(service, **kwargs)
    return result.terminal_event if result.status == "completed" else None


async def _compact_attempt(
    service: ContextCompactionService,
    **kwargs,
) -> ContextCompactionAttemptResult:
    try:
        result = await service.compact(
            target_model_target=_target(service),
            **kwargs,
        )
    except ContextCompactionInvocationFailed as exc:
        if isinstance(exc.__cause__, Exception):
            raise exc.__cause__
        raise
    return result


def _runtime_request_text(context: LLMContext) -> str:
    request = next(
        message
        for message in context.messages
        if message.role is MessageRole.RUNTIME_REQUEST
    )
    return request.content[0]


class _CompactionRuntimeSessionTestFacade:
    """Keep legacy source fixtures while exercising the RuntimeSession port."""

    def __init__(self, *, backing_runtime_session, event_log: InMemoryEventLog) -> None:
        self._backing_runtime_session = backing_runtime_session
        self.event_log = event_log
        self.runtime_session_id = event_log.runtime_session_id

    def __getattr__(self, name: str):
        return getattr(self._backing_runtime_session, name)

    async def write_event_with_deadline(
        self,
        event,
        *,
        deadline_monotonic,
        state=None,
        publication_terminal_maintenance_lease=None,
    ) -> EventWriteResult:
        return await self.write_events_with_deadline(
            (event,),
            deadline_monotonic=deadline_monotonic,
            publication_terminal_maintenance_lease=(
                publication_terminal_maintenance_lease
            ),
        )

    async def write_events_with_deadline(
        self,
        events,
        *,
        deadline_monotonic,
        state=None,
        publication_terminal_maintenance_lease=None,
    ) -> EventWriteResult:
        del deadline_monotonic, state, publication_terminal_maintenance_lease
        candidates = tuple(events)
        confirmation = self.event_log.confirm_stored_batch(candidates)
        if confirmation.disposition == "full":
            assert confirmation.confirmed_full_batch is not None
            receipt = confirmation.confirmed_full_batch.receipt
        elif confirmation.disposition == "none":
            receipt = self.event_log.commit_batch(candidates)
        else:
            raise AssertionError(
                "compaction fixture requires an exact FULL or NONE batch; "
                f"got {confirmation.disposition}"
            )
        stored_events = receipt.owned_stored_events
        return committed_event_write_result_fixture(
            stored_events,
            runtime_session_id=self.runtime_session_id,
            receipt=receipt,
            publisher_enqueued_through_sequence=stored_events[-1].sequence,
        )


def _compaction_service(**kwargs: Any) -> ContextCompactionService:
    """Build compaction tests on the same RuntimeSession-owned model path as production."""

    runtime_session = kwargs.get("runtime_session")
    if runtime_session is None:
        runtime_session_id = str(kwargs["event_log"].runtime_session_id)
        kwargs["runtime_session_id"] = runtime_session_id
        backing_runtime_session = in_memory_runtime_session(
            Path.cwd(),
            runtime_session_id=runtime_session_id,
        )
        runtime_session = _CompactionRuntimeSessionTestFacade(
            backing_runtime_session=backing_runtime_session,
            event_log=kwargs["event_log"],
        )
        kwargs["runtime_session"] = runtime_session
    if kwargs.get("event_commit_port") is None:
        kwargs["event_commit_port"] = RuntimeSessionCompactionEventCommitPort(
            runtime_session
        )
    return ContextCompactionService(**kwargs)


def _ctx(label: str) -> EventContext:
    return EventContext(
        run_id=f"run:{label}",
        turn_id=f"turn:{label}",
        reply_id=f"reply:{label}",
    )


def _accepted_reply_events(
    ctx: EventContext,
    assistant_text: str,
) -> tuple:
    model_start, model_end, disposition = _accepted_reply_control(ctx)
    return (
        ReplyStartEvent(
            id=model_start.recovery_plan.reply_start_event_id,
            **ctx.event_fields(),
            name="assistant",
        ),
        model_start,
        make_text_block_start_event(
            **ctx.event_fields(), block_id=f"text:{ctx.run_id}"
        ),
        make_text_block_segment_event(
            **ctx.event_fields(),
            block_id=f"text:{ctx.run_id}",
            delta=assistant_text,
        ),
        make_text_block_end_event(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        model_end,
        ReplyEndEvent(
            id=model_start.recovery_plan.stable_reply_end_event_id,
            **ctx.event_fields(),
            model_terminal_outcome="completed",
        ),
        disposition,
    )


def _accepted_reply_control(ctx: EventContext) -> tuple:
    model_start = ModelCallStartEvent(
        **ctx.event_fields(),
        **model_call_start_fields(),
    )
    model_end = ModelCallEndEvent(
        id=model_start.recovery_plan.stable_model_call_end_event_id,
        **ctx.event_fields(),
        **model_call_end_fields(resolved_call=model_start.resolved_call),
    )
    disposition_fields = {
        "id": (
            "model_call_control_disposition:"
            f"{ctx.run_id}:{model_start.resolved_call.resolved_model_call_id}:1"
        ),
        **ctx.event_fields(),
        "resolved_model_call_id": model_start.resolved_call.resolved_model_call_id,
        "model_call_start_event_id": model_start.id,
        "model_call_end_event_id": model_end.id,
        "model_call_index": 1,
        "source_result_fingerprint": "sha256:" + "e" * 64,
        "run_execution_activation": model_start.recovery_plan.run_execution_activation,
        "disposition": ModelCallControlDisposition.ACCEPTED,
        "termination_intent": None,
        "recovery_reason_code": None,
    }
    provisional = ModelCallControlDispositionResolvedEvent.model_construct(
        **disposition_fields,
        event_fingerprint="pending",
    )
    payload = provisional.model_dump(
        mode="json", exclude={"event_fingerprint", "sequence"}
    )
    disposition = ModelCallControlDispositionResolvedEvent(
        **payload,
        event_fingerprint=sha256_fingerprint(
            "model-call-control-disposition-event:v1", payload
        ),
    )
    return model_start, model_end, disposition


def _suppressed_reply_events(
    ctx: EventContext,
    assistant_text: str,
) -> tuple:
    model_start, model_end, _accepted = _accepted_reply_control(ctx)
    intent_payload = {
        "schema_version": "run_termination_intent_attribution.v1",
        "intent_id": f"termination-intent:{ctx.run_id}",
        "kind": "user_stop",
        "requested_at_utc": "2026-07-14T00:00:00Z",
        "requester_id": "test-user",
        "target_run_execution_activation_fingerprint": (
            model_start.recovery_plan.run_execution_activation.activation_fingerprint
        ),
    }
    intent = RunTerminationIntentAttributionFact(
        **intent_payload,
        attribution_fingerprint=sha256_fingerprint(
            "run-termination-intent-attribution:v1", intent_payload
        ),
    )
    disposition_payload = {
        "id": (
            "model_call_control_disposition:"
            f"{ctx.run_id}:{model_start.resolved_call.resolved_model_call_id}:1"
        ),
        **ctx.event_fields(),
        "resolved_model_call_id": model_start.resolved_call.resolved_model_call_id,
        "model_call_start_event_id": model_start.id,
        "model_call_end_event_id": model_end.id,
        "model_call_index": 1,
        "source_result_fingerprint": "sha256:" + "d" * 64,
        "run_execution_activation": model_start.recovery_plan.run_execution_activation,
        "disposition": ModelCallControlDisposition.SUPPRESSED_BY_TERMINATION,
        "termination_intent": intent,
        "recovery_reason_code": None,
    }
    provisional = ModelCallControlDispositionResolvedEvent.model_construct(
        **disposition_payload,
        event_fingerprint="pending",
    )
    event_payload = provisional.model_dump(
        mode="json", exclude={"event_fingerprint", "sequence"}
    )
    disposition = ModelCallControlDispositionResolvedEvent(
        **event_payload,
        event_fingerprint=sha256_fingerprint(
            "model-call-control-disposition-event:v1", event_payload
        ),
    )
    return (
        ReplyStartEvent(
            id=model_start.recovery_plan.reply_start_event_id,
            **ctx.event_fields(),
            name="assistant",
        ),
        model_start,
        make_text_block_start_event(
            **ctx.event_fields(), block_id=f"text:{ctx.run_id}"
        ),
        make_text_block_segment_event(
            **ctx.event_fields(),
            block_id=f"text:{ctx.run_id}",
            delta=assistant_text,
        ),
        make_text_block_end_event(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        model_end,
        ReplyEndEvent(
            id=model_start.recovery_plan.stable_reply_end_event_id,
            **ctx.event_fields(),
            model_terminal_outcome="completed",
        ),
        disposition,
    )


def test_suppressed_model_reply_is_audit_only_for_preflight_compaction() -> None:
    ctx = _ctx("suppressed-compaction-source")
    suppressed_text = "SUPPRESSED OUTPUT MUST NOT REACH FUTURE CONTEXT"
    events = [
        RunStartEvent(
            **ctx.event_fields(),
            **run_start_permission_fields(ctx.run_id, user_input="stop this reply"),
            user_input_chars=len("stop this reply"),
            metadata={"user_input": "stop this reply"},
        ),
        *_suppressed_reply_events(ctx, suppressed_text),
    ]

    observation = compaction_service_module.build_compaction_observation_text(events)
    visible_messages = compaction_service_module.model_visible_messages_from_events(
        events
    )

    assert suppressed_text not in observation
    assert all(
        suppressed_text not in str(message.model_dump(mode="json"))
        for message in visible_messages
    )

    log = InMemoryEventLog(runtime_session_id="runtime:suppressed-compaction")
    log.extend(
        [
            *events,
            RunEndEvent(
                **ctx.event_fields(),
                **run_end_contract_fields(
                    ctx.run_id,
                    status="aborted",
                    abort_kind="user_stop",
                ),
                status="aborted",
                stop_reason="aborted",
                abort_kind="user_stop",
            ),
        ]
    )
    transcript = rebuild_prior_messages_bounded(
        log,
        archive=InMemoryArchiveStore(),
        session_id=log.runtime_session_id,
        deadline_monotonic=monotonic() + 1,
    )
    assert all(
        suppressed_text not in str(message.model_dump(mode="json"))
        for message in transcript.messages
    )


def _append_turn(
    log: InMemoryEventLog, label: str, user_input: str, assistant_text: str
) -> None:
    if log.next_sequence() == 1 and log.runtime_session_id == "in-memory":
        log.bind_runtime_session_id("runtime:test")
    ctx = _ctx(label)
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(
                    ctx.run_id,
                    user_input=user_input,
                    mcp_installation_owner_runtime_session_id=(log.runtime_session_id),
                ),
                user_input_chars=len(user_input),
                metadata={"user_input": user_input},
            ),
            *_accepted_reply_events(ctx, assistant_text),
        ]
    )


def test_bounded_prior_transcript_reads_all_replies_in_one_snapshot() -> None:
    class CountingEventLog(InMemoryEventLog):
        reply_snapshot_reads = 0

        def read_raw_replies_snapshot(self, *args, **kwargs):
            self.reply_snapshot_reads += 1
            return super().read_raw_replies_snapshot(*args, **kwargs)

        def read_raw_reply_events(self, *args, **kwargs):
            raise AssertionError("bounded PRE_RUN must not issue per-reply reads")

    log = CountingEventLog(runtime_session_id="runtime:reply-batch")
    _append_turn(log, "batch-one", "first", "one")
    _append_turn(log, "batch-two", "second", "two")

    transcript = rebuild_prior_messages_bounded(
        log,
        archive=InMemoryArchiveStore(),
        session_id=log.runtime_session_id,
        deadline_monotonic=monotonic() + 1,
    )

    assert log.reply_snapshot_reads == 1
    assert [
        block.text
        for message in transcript.messages
        for block in message.content
        if isinstance(block, TextBlock)
    ] == [
        "first",
        "one",
        "second",
        "two",
    ]


async def _emit_turn(
    runtime_session, label: str, user_input: str, assistant_text: str
) -> None:
    ctx = _ctx(label)
    llm_runtime = _llm_runtime(CompactScriptedTransport(assistant_text))
    target = llm_runtime.resolve_target(role=ModelRole.PRO)
    open_test_root_rollout_run(
        runtime_session,
        event_context=ctx,
        model_target=target.fact,
        user_input=user_input,
    )
    if runtime_session.materialization_account_store.snapshot() is None:
        runtime_session._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=ctx.run_id
        )
    call = llm_runtime.resolve_call(
        target=target,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    context = bind_test_context(
        call,
        test_llm_context(
            messages=(LLMMessage.user(user_input),),
            context_id=f"context:test:{label}",
            model_call_index=1,
        ),
    )
    activation = make_test_run_execution_activation()
    provider_input = await (
        runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
            call=call,
            context=context,
            event_context=ctx,
            operation_kind="direct_model_call",
            operation_id=call.fact.resolved_model_call_id,
        )
    )
    context = bind_test_provider_input_context(call, provider_input, context)
    start_bundle = prepare_model_lifecycle_start_bundle(
        call=call,
        context=context,
        event_context=ctx,
        runtime_session=runtime_session,
        lifecycle_kind="main_assistant_reply",
        run_execution_activation=activation,
        provider_input_start_bundle=provider_input,
    )
    result = await llm_runtime.start_stream(
        call=call,
        context=context,
        event_context=ctx,
        start_bundle=start_bundle,
        commit_port=RuntimeSessionModelStreamEventCommitPort(
            runtime_session=runtime_session,
        ),
        execution_registry=runtime_session.model_stream_execution_registry,
    ).wait_result()
    disposition_fields = {
        "id": (
            "model_call_control_disposition:"
            f"{ctx.run_id}:{result.resolved_model_call_id}:1"
        ),
        **ctx.event_fields(),
        "resolved_model_call_id": result.resolved_model_call_id,
        "model_call_start_event_id": result.model_call_start_event_id,
        "model_call_end_event_id": result.model_call_end_event_id,
        "model_call_index": 1,
        "source_result_fingerprint": result.result_fingerprint,
        "run_execution_activation": activation,
        "disposition": ModelCallControlDisposition.ACCEPTED,
        "termination_intent": None,
        "recovery_reason_code": None,
    }
    provisional = ModelCallControlDispositionResolvedEvent.model_construct(
        **disposition_fields,
        event_fingerprint="pending",
    )
    payload = provisional.model_dump(
        mode="json", exclude={"event_fingerprint", "sequence"}
    )
    await runtime_session.emit(
        ModelCallControlDispositionResolvedEvent(
            **payload,
            event_fingerprint=sha256_fingerprint(
                "model-call-control-disposition-event:v1", payload
            ),
        )
    )


def _current_tail_state(
    runtime_session_id: str, *, run_id: str = "run:current"
) -> RunActivationWorkingState:
    state = RunActivationWorkingState(session_id=runtime_session_id, run_id=run_id)
    state.messages = [
        UserMsg(
            name="user",
            content="current request",
            id=f"user-message:{run_id}",
            metadata={"run_id": run_id},
        ),
        AssistantMsg(
            name="assistant",
            id=state.reply_id,
            content=[
                ToolCallBlock(
                    id="call:current",
                    name="terminal",
                    input='{"command":"printf current"}',
                    state=ToolCallState.FINISHED,
                )
            ],
        ),
        Msg(
            role="tool_result",
            name="terminal",
            id="tool-result-message:call:current",
            content=[
                ToolResultBlock(
                    id="call:current",
                    name="terminal",
                    state=ToolResultState.SUCCESS,
                    output=[TextBlock(text="current tool result")],
                )
            ],
        ),
    ]
    state.pending_tool_calls = [
        ToolCallBlock(
            id="call:current",
            name="terminal",
            input='{"command":"printf current"}',
            state=ToolCallState.FINISHED,
        )
    ]
    state.tool_results = [
        ToolResultBlock(
            id="call:current",
            name="terminal",
            state=ToolResultState.SUCCESS,
            output=[TextBlock(text="current tool result")],
        )
    ]
    state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
    return state


def _protected_current_run_messages(
    state: RunActivationWorkingState,
) -> tuple[LLMMessage, ...]:
    """Minimal compaction fixture; typed renderer behavior is tested separately."""

    user = next(message for message in state.messages if message.role == "user")
    result = next(
        message for message in state.messages if message.role == "tool_result"
    )
    user_text = "\n".join(
        block.text for block in user.content if isinstance(block, TextBlock)
    )
    result_block = next(
        block for block in result.content if isinstance(block, ToolResultBlock)
    )
    result_text = "\n".join(
        block.text for block in result_block.output if isinstance(block, TextBlock)
    )
    return (
        LLMMessage.user(user_text),
        LLMMessage.tool_result(result_text, tool_call_id=result_block.id),
    )


def _llm_runtime(
    transport: CompactScriptedTransport,
    *,
    pro_limits=None,
    flash_limits=None,
) -> LLMRuntime:
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(
        config=test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api=transport.api,
            pro_limits=pro_limits,
            flash_limits=flash_limits,
        ),
        registry=registry,
    )


class _FakeCompactionServiceBase:
    async def drain_pending_terminalizations(self, *, timeout_seconds: float) -> None:
        del timeout_seconds

    async def stop_post_completion_extension_admission_and_drain(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        del deadline_monotonic


def _fake_compaction_attempt(
    terminal_event: ContextCompactionCompletedEvent
    | ContextCompactionFailedEvent
    | None = None,
) -> ContextCompactionAttemptResult:
    admitted_at = monotonic()
    status = (
        "completed"
        if isinstance(terminal_event, ContextCompactionCompletedEvent)
        else "failed"
        if isinstance(terminal_event, ContextCompactionFailedEvent)
        else "not_attempted"
    )
    return ContextCompactionAttemptResult(
        attempt_id=f"context_compaction_attempt:{uuid4().hex}",
        compaction_id=(
            terminal_event.compaction_id if terminal_event is not None else None
        ),
        terminal_event_deadline_budget=(
            build_runtime_event_deadline_budget(
                admitted_at_monotonic=admitted_at,
                total_timeout_seconds=5.0,
                terminal_reserve_seconds=1.0,
            )
            if terminal_event is not None
            else None
        ),
        publication_terminalization_scope=None,
        status=status,
        not_attempted_reason="below_threshold" if terminal_event is None else None,
        core_committed_events=((terminal_event,) if terminal_event is not None else ()),
        terminal_event=terminal_event,
        committed_through_sequence=(
            terminal_event.sequence if terminal_event is not None else None
        ),
        publication_summary=(
            "completed" if terminal_event is not None else "not_applicable"
        ),
        publication_errors=(),
    )


class _FakeHostCompactionService(_FakeCompactionServiceBase):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def compact(self, **kwargs):
        self.calls.append(kwargs)
        ctx = _ctx("compaction:host")
        return _fake_compaction_attempt(
            ContextCompactionCompletedEvent(
                **ctx.event_fields(),
                **compaction_completed_contract_fields(
                    estimated_tokens_before=200_001,
                    estimated_tokens_after=4_000,
                ),
                compaction_id="context_compaction:host",
                trigger="manual",
                reason="user_requested",
                window_number=1,
                summary_artifact_id="context_compaction_host:summary",
                window_id="context_window:host",
                summary_chars=1,
                threshold_tokens=200_000,
                through_sequence=10,
                keep_after_sequence=10,
            )
        )

    async def compact_if_needed(self, **kwargs) -> ContextCompactionAttemptResult:
        self.calls.append({"method": "compact_if_needed", **kwargs})
        return _fake_compaction_attempt()


def _host_interaction_fixture(
    tmp_path: Path,
    *,
    replies: list[dict[str, object]],
    compaction_service: _FakeHostCompactionService | None = None,
    ask_permissions: bool = False,
):
    runtime_wiring = build_component_runtime_wiring(tmp_path)
    fake = compaction_service or _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    transport = HostInteractionTransport(replies)
    agent = build_test_agent_runtime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_llm_runtime(transport),
        permission_policy=(
            preset_to_policy(PermissionMode.ASK_PERMISSIONS)
            if ask_permissions
            else preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
        ),
        enable_subagents=False,
    )
    session = HostSession(
        host_session_id=f"host:interaction:{uuid4().hex}",
        conversation_id=f"conversation:interaction:{uuid4().hex}",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )
    return session, agent, transport, fake


class _FakeFailingAutoCompactionService(_FakeCompactionServiceBase):
    def __init__(self, event_log: InMemoryEventLog) -> None:
        self.event_log = event_log

    async def compact_if_needed(self, **kwargs) -> ContextCompactionAttemptResult:
        ctx = _ctx("compaction:auto:failed")
        failed = self.event_log.append(
            ContextCompactionFailedEvent(
                **ctx.event_fields(),
                **compaction_failed_contract_fields(),
                compaction_id="context_compaction:failed",
                trigger="auto",
                reason=str(kwargs.get("reason", "context_threshold")),
                window_number=1,
                window_id="context_window:failed",
                threshold_tokens=200_000,
                through_sequence=10,
                keep_after_sequence=5,
                error_type="RuntimeError",
                message="boom",
            )
        )
        return _fake_compaction_attempt(failed)


class _FakePreflightBoundaryCompactionService(_FakeCompactionServiceBase):
    def __init__(
        self,
        runtime_session,
    ) -> None:
        self.runtime_session = runtime_session
        self.event_log = runtime_session.event_log
        self.archive = runtime_session.archive
        self.runtime_session_id = runtime_session.runtime_session_id
        self.calls: list[dict[str, object]] = []

    async def compact_if_needed(self, **kwargs) -> ContextCompactionAttemptResult:
        self.calls.append({"method": "compact_if_needed", **kwargs})
        ctx = _ctx("compaction:preflight:completed")
        artifact_id = "context_compaction_preflight:summary"
        self.archive.put_text(
            artifact_id,
            "Preflight summary sentinel.",
            session_id=self.runtime_session_id,
            metadata={"kind": "context_compaction_summary", "do_not_write_back": True},
        )
        through_sequence = self.event_log.next_sequence() - 1
        started_event_id = "context_compaction_started:preflight"
        completed_event_id = "context_compaction_completed:preflight"
        started_fields = compaction_started_contract_fields(
            estimated_tokens_before=200_001,
        )
        started_fields["terminal_event_id"] = completed_event_id
        await self.runtime_session.write_event(
            ContextCompactionStartedEvent(
                id=started_event_id,
                **ctx.event_fields(),
                **started_fields,
                compaction_id="context_compaction:preflight",
                trigger="auto",
                reason=str(kwargs.get("reason", "preflight_context_threshold")),
                window_number=1,
                window_id="context_window:preflight",
                threshold_tokens=200_000,
                through_sequence=through_sequence,
                keep_after_sequence=through_sequence,
            )
        )
        completed_fields = compaction_completed_contract_fields(
            estimated_tokens_before=200_001,
            estimated_tokens_after=4_000,
        )
        completed_fields["started_event_id"] = started_event_id
        completed_result = await self.runtime_session.write_event(
            ContextCompactionCompletedEvent(
                id=completed_event_id,
                **ctx.event_fields(),
                **completed_fields,
                compaction_id="context_compaction:preflight",
                trigger="auto",
                reason=str(kwargs.get("reason", "preflight_context_threshold")),
                window_number=1,
                window_id="context_window:preflight",
                summary_artifact_id=artifact_id,
                summary_chars=27,
                threshold_tokens=200_000,
                through_sequence=through_sequence,
                keep_after_sequence=through_sequence,
            )
        )
        completed = completed_result.committed_events[-1]
        assert isinstance(completed, ContextCompactionCompletedEvent)
        return _fake_compaction_attempt(completed)


class _FakeOneShotPreflightBoundaryCompactionService(
    _FakePreflightBoundaryCompactionService
):
    async def compact_if_needed(self, **kwargs) -> ContextCompactionAttemptResult:
        if not self.calls:
            return await super().compact_if_needed(**kwargs)
        self.calls.append({"method": "compact_if_needed", **kwargs})
        return _fake_compaction_attempt()


def test_context_compaction_events_round_trip() -> None:
    ctx = _ctx("compaction:event")
    started = ContextCompactionStartedEvent(
        **ctx.event_fields(),
        **compaction_started_contract_fields(estimated_tokens_before=200_001),
        compaction_id="context_compaction:test",
        trigger="auto",
        reason="context_threshold",
        window_number=1,
        window_id="context_window:1",
        threshold_tokens=200_000,
        through_sequence=10,
        keep_after_sequence=10,
    )
    completed = ContextCompactionCompletedEvent(
        **ctx.event_fields(),
        **compaction_completed_contract_fields(
            estimated_tokens_before=200_001,
            estimated_tokens_after=4_000,
        ),
        compaction_id="context_compaction:test",
        trigger="auto",
        reason="context_threshold",
        window_number=1,
        window_id="context_window:1",
        summary_artifact_id="artifact:summary",
        summary_chars=12,
        threshold_tokens=200_000,
        through_sequence=10,
        keep_after_sequence=10,
        included_run_ids=["run:a"],
        included_artifact_ids=["artifact:a"],
    )

    assert load_agent_event(dump_agent_event(started)) == started
    assert load_agent_event(dump_agent_event(completed)) == completed


def test_strip_compaction_analysis_keeps_summary_only() -> None:
    raw = (
        "<analysis>private checklist</analysis>\n<summary>\nUseful handoff.\n</summary>"
    )

    assert strip_compaction_analysis(raw) == "Useful handoff."


def test_strip_compaction_analysis_rejects_unclosed_private_blocks() -> None:
    assert strip_compaction_analysis("<analysis>private checklist with no close") == ""
    assert strip_compaction_analysis("<summary>official handoff with no close") == ""


def test_compaction_prompt_preserves_yielded_terminal_process_continuation() -> None:
    prompt = production_compaction_prompt()

    assert "process_id" in prompt
    assert "long-running or background process" in prompt
    assert "continue with terminal_process" in prompt
    assert "rather than restarting the command" in prompt


def test_compaction_metadata_only_input_is_smaller_than_full_observation_input() -> (
    None
):
    log = InMemoryEventLog()
    _append_turn(log, "dense", "plain user input", "assistant reply")
    ctx = _ctx("dense")
    log.extend(
        [
            make_tool_call_start_event(
                **ctx.event_fields(), tool_call_id="call:one", tool_call_name="terminal"
            ),
            *[
                make_tool_call_arguments_segment_event(
                    **ctx.event_fields(),
                    tool_call_id="call:one",
                    delta='{"cmd":"echo hi"}'[:1],
                )
                for _ in range(50)
            ],
            make_tool_call_end_event(**ctx.event_fields(), tool_call_id="call:one"),
        ]
    )
    service = _compaction_service(
        event_log=log,
        archive=InMemoryArchiveStore(),
        llm_runtime=_llm_runtime(CompactScriptedTransport("<summary>ok</summary>")),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    plan = service._build_plan(
        log.iter(),
        compaction_id="context_compaction:00000000000000000000000000000000",
        target_model_target=_target(service),
        force=True,
    )

    assert plan is not None
    assert len(build_metadata_only_compaction_input(plan)) < len(
        build_compaction_input(plan)
    )


def test_compaction_plan_collects_tool_result_artifact_ids() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(
        log, "artifact", "user asked for a web search", "tool produced a large artifact"
    )
    log.append(
        ToolResultEndEvent(
            **_ctx("artifact").event_fields(),
            **tool_result_end_contract_fields("call:firecrawl", tool_name="firecrawl"),
            tool_call_id="call:firecrawl",
            state=ToolResultState.SUCCESS,
            metadata={
                "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
            },
            artifacts=[
                ToolResultArtifactRef(
                    artifact_id="artifact:tool-result:run:call:firecrawl:output:0",
                    role="output",
                    media_type="text/markdown; charset=utf-8",
                    size_bytes=1234,
                )
            ],
        )
    )
    transport = CompactScriptedTransport(
        "<summary>Search result was summarized.</summary>"
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    event = asyncio.run(
        _compact(service, trigger="manual", reason="user_requested", force=True)
    )

    assert event is not None
    assert event.included_artifact_ids == [
        "artifact:tool-result:run:call:firecrawl:output:0"
    ]


def test_manual_context_compaction_writes_summary_artifact_and_events() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "one", "first request", "first reply")
    transport = CompactScriptedTransport(
        "<analysis>draft</analysis><summary>Task state: first request was handled.</summary>"
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    event = asyncio.run(
        _compact(service, trigger="manual", reason="user_requested", force=True)
    )

    assert event is not None
    assert event.trigger == "manual"
    assert event.summary_artifact_id in archive.blobs
    assert (
        archive.get_text(event.summary_artifact_id, session_id="runtime:test")
        == "Task state: first request was handled."
    )
    assert any(
        isinstance(stored, ContextCompactionStartedEvent) for stored in log.iter()
    )
    assert any(
        isinstance(stored, ContextCompactionCompletedEvent) for stored in log.iter()
    )
    assert transport.contexts
    assert transport.contexts[0].tools == ()
    assert "Do NOT call any tools" in (transport.contexts[0].system_prompt or "")


def test_completed_extraction_request_batch_retires_terminal_owner(tmp_path) -> None:
    class YieldingCompactTransport(CompactScriptedTransport):
        async def stream(self, **kwargs) -> AsyncIterator:
            await asyncio.sleep(0.05)
            async for item in super().stream(**kwargs):
                yield item

    async def scenario() -> None:
        wiring = build_component_runtime_wiring(tmp_path)
        runtime_session = wiring.runtime_session
        await _emit_turn(
            runtime_session,
            "extension-batch",
            "I prefer compact progress reports.",
            "Preference acknowledged.",
        )
        extension = MemoryCompactionPostCompletionExtension(
            archive=wiring.archive,
            runtime_session_id=runtime_session.runtime_session_id,
            memory_domain=MemoryDomainContext(
                memory_domain_id="u_test",
                workspace_kind="transient",
            ),
            resolved_model_target_factory=test_resolved_target_fact,
            physical_executor=auxiliary_io_executor(),
        )
        service = _compaction_service(
            event_log=wiring.event_log,
            archive=wiring.archive,
            llm_runtime=_llm_runtime(
                YieldingCompactTransport("<summary>Stable handoff.</summary>")
            ),
            runtime_session_id=runtime_session.runtime_session_id,
            runtime_session=runtime_session,
            post_completion_extension=extension,
            policy=ContextCompactionPolicy(min_events_after_last_compact=1),
        )

        result = await service.compact(
            target_model_target=_target(service),
            trigger="manual",
            reason="user_requested",
            force=True,
        )

        assert result.status == "completed"
        assert service.pending_terminalization_count == 0
        assert any(
            event.type == "CONTEXT_COMPACTION_MEMORY_EXTRACTION_REQUESTED"
            for event in wiring.event_log.iter()
        )
        await extension.stop_admission_and_drain(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(scenario())


def test_cancelled_compaction_after_started_commits_stable_failed_terminal() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "cancel", "first request", "first reply")
    transport = BlockingCompactTransport()
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            _compact(service, trigger="manual", reason="user_requested", force=True)
        )
        await transport.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    started = next(
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionStartedEvent)
    )
    failed = next(
        event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)
    )
    assert failed.id == started.terminal_event_id
    assert failed.started_event_id == started.id
    assert failed.termination_kind == "cancelled"


def test_cancelled_started_write_that_commits_late_remains_service_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "late-started", "first request", "first reply")
    entered = asyncio.Event()
    release = asyncio.Event()
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    runtime_session = service.runtime_session
    assert runtime_session is not None
    base_write = runtime_session.write_event_with_deadline

    async def write_late(event, **kwargs):
        if isinstance(event, ContextCompactionStartedEvent):
            entered.set()
            await release.wait()
        return await base_write(event, **kwargs)

    monkeypatch.setattr(runtime_session, "write_event_with_deadline", write_late)

    async def scenario() -> None:
        task = asyncio.create_task(
            _compact(service, trigger="manual", reason="user_requested", force=True)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.pending_terminalization_count == 1
        assert not any(
            isinstance(event, ContextCompactionStartedEvent) for event in log.iter()
        )

        release.set()
        await service.drain_pending_terminalizations(timeout_seconds=1.0)

    asyncio.run(scenario())
    assert service.pending_terminalization_count == 0
    started = [
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionStartedEvent)
    ]
    failed = [
        event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)
    ]
    assert len(started) == 1
    assert len(failed) == 1
    assert failed[0].started_event_id == started[0].id
    assert failed[0].id == started[0].terminal_event_id


def test_compaction_confirmation_failure_transfers_late_commit_owner() -> None:
    log = InMemoryEventLog()
    entered = asyncio.Event()
    release = asyncio.Event()

    class ConfirmationUnavailableRuntime:
        async def write_event_with_deadline(
            self,
            event,
            *,
            deadline_monotonic,
            state=None,
            publication_terminal_maintenance_lease=None,
        ):
            del deadline_monotonic, state, publication_terminal_maintenance_lease
            entered.set()
            await release.wait()
            receipt = log.commit_batch((event,))
            stored = receipt.owned_stored_events[0]
            return committed_event_write_result_fixture(
                (stored,),
                runtime_session_id=log.runtime_session_id,
                receipt=receipt,
                publisher_enqueued_through_sequence=stored.sequence,
            )

        def confirm_event_batch(self, _events):
            raise RuntimeError("confirmation store unavailable")

    event = typed_non_transcript_event(
        label="compaction_confirmation_unknown_probe",
        context=_ctx("compaction:confirmation-unknown"),
    )
    port = RuntimeSessionCompactionEventCommitPort(ConfirmationUnavailableRuntime())
    deadline_budget = build_runtime_event_deadline_budget(
        admitted_at_monotonic=monotonic(),
        total_timeout_seconds=5.0,
        terminal_reserve_seconds=1.0,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            port.commit_event(event, deadline_budget=deadline_budget)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(CompactionCommitPendingAfterCancellation) as raised:
            await task
        assert log.get_by_id(event.id) is None
        release.set()
        result = await raised.value.pending.resolve(timeout_seconds=1.0)
        assert result.committed_event.id == event.id

    asyncio.run(scenario())
    assert log.get_by_id(event.id) is not None


def test_orphan_compaction_started_is_owned_and_recovered_before_reuse() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    started_fields = compaction_started_contract_fields()
    started_fields["terminal_event_id"] = "context_compaction_terminal:orphan"
    started = ContextCompactionStartedEvent(
        id="context_compaction_started:orphan",
        **_ctx("compaction:orphan").event_fields(),
        **started_fields,
        compaction_id="context_compaction:orphan",
        trigger="auto",
        reason="preflight_context_threshold",
        window_number=1,
        window_id="context_window:orphan",
        threshold_tokens=100,
        through_sequence=1,
        keep_after_sequence=1,
    )
    log.append(started)
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
        runtime_session_id="runtime:test",
    )
    assert service.pending_terminalization_count == 1

    asyncio.run(service.drain_pending_terminalizations(timeout_seconds=1.0))

    assert service.pending_terminalization_count == 0
    failed = next(
        event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)
    )
    assert failed.id == started.terminal_event_id
    assert failed.started_event_id == started.id
    assert failed.failure_stage == "recovery_terminalization"
    assert failed.termination_kind == "recovered_interrupted"


def test_terminal_commit_failure_keeps_bounded_compaction_owner_for_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "owner", "first request", "first reply")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(
            CompactScriptedTransport("<summary>stable summary</summary>")
        ),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    runtime_session = service.runtime_session
    assert runtime_session is not None
    base_write = runtime_session.write_event_with_deadline

    async def fail_terminal(event, **kwargs):
        if isinstance(
            event,
            (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
        ):
            raise RuntimeError("terminal store unavailable")
        return await base_write(event, **kwargs)

    monkeypatch.setattr(
        runtime_session,
        "write_event_with_deadline",
        fail_terminal,
    )

    with pytest.raises(RuntimeError, match="terminal store unavailable"):
        asyncio.run(
            _compact(service, trigger="manual", reason="user_requested", force=True)
        )
    assert service.pending_terminalization_count == 1
    assert any(isinstance(event, ContextCompactionStartedEvent) for event in log.iter())
    assert not any(
        isinstance(
            event, (ContextCompactionCompletedEvent, ContextCompactionFailedEvent)
        )
        for event in log.iter()
    )

    monkeypatch.setattr(
        runtime_session,
        "write_event_with_deadline",
        base_write,
    )
    asyncio.run(service.drain_pending_terminalizations(timeout_seconds=1.0))
    assert service.pending_terminalization_count == 0
    terminals = [
        event
        for event in log.iter()
        if isinstance(
            event, (ContextCompactionCompletedEvent, ContextCompactionFailedEvent)
        )
    ]
    assert len(terminals) == 1


def test_compaction_terminal_write_deadline_starts_after_slow_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [monotonic()]

    class SlowClockTransport(CompactScriptedTransport):
        async def stream(self, **kwargs) -> AsyncIterator:
            clock[0] += 45.0
            async for item in super().stream(**kwargs):
                yield item

    monkeypatch.setattr(
        compaction_service_module,
        "monotonic",
        lambda: clock[0],
    )
    admitted: dict[
        type[object],
        list[tuple[float, object]],
    ] = {}
    original_commit = RuntimeSessionCompactionEventCommitPort.commit_event

    async def recording_commit(self, event, **kwargs):
        budget = kwargs["deadline_budget"]
        admitted.setdefault(type(event), []).append(
            (budget.admitted_at_monotonic, budget)
        )
        return await original_commit(self, event, **kwargs)

    monkeypatch.setattr(
        RuntimeSessionCompactionEventCommitPort,
        "commit_event",
        recording_commit,
    )
    log = InMemoryEventLog()
    _append_turn(log, "slow-model", "request", "reply")
    service = _compaction_service(
        event_log=log,
        archive=InMemoryArchiveStore(),
        llm_runtime=_llm_runtime(
            SlowClockTransport("<summary>stable summary</summary>")
        ),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    attempt = asyncio.run(
        _compact_attempt(
            service,
            trigger="manual",
            reason="user_requested",
            force=True,
        )
    )

    completed = attempt.terminal_event
    assert isinstance(completed, ContextCompactionCompletedEvent)
    started_at, _started_budget = admitted[ContextCompactionStartedEvent][0]
    completed_at, completed_budget = admitted[ContextCompactionCompletedEvent][0]
    assert completed_at - started_at >= 45.0
    assert attempt.terminal_event_deadline_budget == completed_budget


def test_compaction_attempt_result_requires_exact_terminal_budget_shape() -> None:
    not_attempted = _fake_compaction_attempt()
    assert not_attempted.terminal_event_deadline_budget is None

    completed_event = ContextCompactionCompletedEvent(
        **_ctx("compaction:attempt-result-budget").event_fields(),
        **compaction_completed_contract_fields(
            estimated_tokens_before=200_001,
            estimated_tokens_after=4_000,
        ),
        compaction_id="context_compaction:attempt-result-budget",
        trigger="manual",
        reason="contract",
        window_number=1,
        window_id="context_window:attempt-result-budget",
        summary_artifact_id="artifact:attempt-result-budget",
        summary_chars=12,
        threshold_tokens=200_000,
        through_sequence=10,
        keep_after_sequence=10,
    )
    completed = _fake_compaction_attempt(completed_event)
    assert completed.terminal_event_deadline_budget is not None

    with pytest.raises(
        ValueError,
        match="terminal compaction result requires its event deadline budget",
    ):
        replace(completed, terminal_event_deadline_budget=None)


def test_repeated_compaction_carries_previous_summary_forward() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "first", "first old request", "first old reply")
    _append_turn(log, "second", "second middle request", "second middle reply")
    transport = CompactScriptedTransport(
        "<summary>FIRST_SENTINEL old context.</summary>"
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1, keep_recent_runs=1
        ),
    )

    first = asyncio.run(
        _compact(service, trigger="manual", reason="user_requested", force=True)
    )
    assert first is not None
    _append_turn(log, "third", "third recent request", "third recent reply")
    transport.text = "<summary>FIRST_SENTINEL old context plus SECOND_SENTINEL middle context.</summary>"

    second = asyncio.run(
        _compact(service, trigger="manual", reason="user_requested", force=True)
    )

    assert second is not None
    second_input = _runtime_request_text(transport.contexts[-1])
    assert "Previous compact summary to carry forward" in second_input
    assert "FIRST_SENTINEL old context." in second_input
    messages = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    rendered = "\n".join(
        block.text
        for message in messages
        for block in message.content
        if hasattr(block, "text")
    )
    assert "FIRST_SENTINEL old context" in rendered
    assert "SECOND_SENTINEL middle context" in rendered
    assert "third recent request" in rendered


def test_malformed_compaction_output_records_failed_event_without_artifact() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "bad", "bad request", "bad reply")
    transport = CompactScriptedTransport("<analysis>private draft without close")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    assert (
        asyncio.run(
            _compact(
                service,
                trigger="auto",
                reason="preflight_context_threshold",
                force=True,
            )
        )
        is None
    )

    events = log.iter()
    assert any(isinstance(event, ContextCompactionFailedEvent) for event in events)
    assert not any(
        isinstance(event, ContextCompactionCompletedEvent) for event in events
    )
    assert not archive.blobs


def test_compaction_model_run_error_fails_even_after_partial_text() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "error", "error request", "error reply")
    transport = CompactErrorAfterTextTransport(
        "<summary>partial summary that must not be stored</summary>"
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    assert (
        asyncio.run(
            _compact(
                service,
                trigger="auto",
                reason="preflight_context_threshold",
                force=True,
            )
        )
        is None
    )

    events = log.iter()
    failed = [
        event for event in events if isinstance(event, ContextCompactionFailedEvent)
    ]
    assert len(failed) == 1
    assert "provider failed mid-summary" in failed[0].message
    assert not any(
        isinstance(event, ContextCompactionCompletedEvent) for event in events
    )
    assert not archive.blobs


def test_rebuild_prior_messages_uses_completed_boundary_and_replays_tail() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old request", "old reply")
    _append_turn(log, "new", "new request", "new reply")
    transport = CompactScriptedTransport(
        "<summary>Old request was completed.</summary>"
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1, keep_recent_runs=1
        ),
    )
    event = asyncio.run(
        _compact(service, trigger="manual", reason="user_requested", force=True)
    )
    assert event is not None

    messages = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    rendered = "\n".join(
        block.text
        for message in messages
        for block in message.content
        if hasattr(block, "text")
    )

    assert "<context-compaction-summary" in rendered
    assert "Old request was completed." in rendered
    assert "new request" in rendered
    assert "new reply" in rendered
    assert "old request" not in rendered


def test_rebuild_prior_messages_before_sequence_uses_mid_turn_boundary_without_replaying_current_run() -> (
    None
):
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old request", "old reply")
    current = _ctx("current")
    current_start = log.append(
        RunStartEvent(
            **current.event_fields(),
            **run_start_permission_fields(current.run_id, user_input="current request"),
            user_input_chars=len("current request"),
            metadata={"user_input": "current request"},
        )
    )
    assert current_start.sequence is not None
    transport = CompactScriptedTransport(
        "<summary>Old request was compacted mid-turn.</summary>"
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1, keep_recent_runs=1
        ),
    )
    runtime_state = RunActivationWorkingState(
        session_id=log.runtime_session_id,
        run_id=current.run_id,
        turn_id=current.turn_id,
        reply_id=current.reply_id,
    )
    runtime_state.run_working_set = SimpleNamespace(
        long_horizon_contract=current_start.long_horizon
    )
    runtime_state.model_tool_progress.active_context_window_id = (
        current_start.long_horizon.initial_window_id
    )
    completed = asyncio.run(
        _compact(
            service,
            trigger="auto",
            reason="mid_turn_context_threshold",
            force=True,
            max_compactable_sequence=current_start.sequence - 1,
            keep_recent_runs_override=1,
            event_metadata={"phase": "mid_turn"},
            runtime_state=runtime_state,
        )
    )
    assert completed is not None
    assert completed.sequence is not None
    assert completed.sequence > current_start.sequence
    compact_input = _runtime_request_text(transport.contexts[-1])
    assert "old request" in compact_input
    assert "current request" not in compact_input

    messages = rebuild_prior_messages_before_sequence(
        log,
        archive=archive,
        session_id="runtime:test",
        before_sequence=current_start.sequence,
    )
    rendered = "\n".join(
        block.text
        for message in messages
        for block in message.content
        if hasattr(block, "text")
    )

    assert "Old request was compacted mid-turn." in rendered
    assert "current request" not in rendered


def test_runtime_context_compactor_rewrites_prefix_and_preserves_current_run_tail(
    tmp_path,
) -> None:
    async def run():
        wiring = build_component_runtime_wiring(tmp_path)
        runtime_session = wiring.runtime_session
        await _emit_turn(
            runtime_session,
            "old-a",
            "old-a request",
            "old-a reply " + ("x" * 10_000),
        )
        await _emit_turn(runtime_session, "old-b", "old-b request", "old-b reply")
        state = _current_tail_state(runtime_session.runtime_session_id)
        current_context = EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
        current_target = _llm_runtime(CompactScriptedTransport("")).resolve_target(
            role=ModelRole.PRO
        )
        open_test_root_rollout_run(
            runtime_session,
            event_context=current_context,
            model_target=current_target.fact,
            user_input="current request",
        )
        current_start = next(
            event
            for event in runtime_session.event_log.iter(run_id=state.run_id)
            if isinstance(event, RunStartEvent)
        )
        state.run_working_set = SimpleNamespace(
            run_start_event_id=current_start.id,
            long_horizon_contract=current_start.long_horizon,
        )
        transport = CompactScriptedTransport(
            "<summary>Old request was summarized.</summary>"
        )
        service = _compaction_service(
            event_log=wiring.event_log,
            archive=wiring.archive,
            llm_runtime=_llm_runtime(transport),
            runtime_session_id=runtime_session.runtime_session_id,
            policy=ContextCompactionPolicy(
                min_events_after_last_compact=1,
                auto_trigger_ratio=0.006,
                post_compaction_target_ratio=0.005,
                max_summary_chars=1_000,
            ),
        )
        state.run_model_target = _target(service)
        compactor = RuntimeContextCompactor(
            event_log=wiring.event_log,
            archive=wiring.archive,
            runtime_session=runtime_session,
            service=service,
        )

        model_visible_messages = [
            *rebuild_prior_messages(
                wiring.event_log,
                archive=wiring.archive,
                session_id=runtime_session.runtime_session_id,
            ),
            *state.messages,
        ]
        result = await compactor.maybe_compact_before_followup(
            state=state,
            model_visible_messages=model_visible_messages,
            protected_model_visible_messages_after=_protected_current_run_messages(
                state
            ),
        )

        assert result.compacted is True
        assert current_start.sequence is not None
        compact_input = _runtime_request_text(transport.contexts[-1])
        assert "old-a request" in compact_input
        assert "old-b request" not in compact_input
        assert "current request" not in compact_input
        assert result.rewritten_messages is not None
        rendered = "\n".join(
            block.text
            for message in result.rewritten_messages
            for block in message.content
            if hasattr(block, "text")
        )
        assert "Old request was summarized." in rendered
        assert "old-a request" not in rendered
        assert "old-b request" in rendered
        assert "current request" in rendered
        assert any(
            isinstance(block, ToolResultBlock)
            and any(
                isinstance(item, TextBlock) and item.text == "current tool result"
                for item in block.output
            )
            for message in result.rewritten_messages
            for block in message.content
        )
        completed = [
            event
            for event in result.events
            if isinstance(event, ContextCompactionCompletedEvent)
        ]
        assert completed
        assert completed[-1].metadata["phase"] == "mid_turn"
        assert (
            completed[-1].metadata["current_run_start_sequence"]
            == current_start.sequence
        )
        assert state.compacted is True

    asyncio.run(run())


def test_runtime_context_compactor_failure_publishes_events_and_keeps_state_messages(
    tmp_path,
) -> None:
    async def run():
        wiring = build_component_runtime_wiring(tmp_path)
        runtime_session = wiring.runtime_session
        await _emit_turn(
            runtime_session,
            "old-a",
            "old-a request",
            "old-a reply " + ("x" * 10_000),
        )
        await _emit_turn(runtime_session, "old-b", "old-b request", "old-b reply")
        state = _current_tail_state(runtime_session.runtime_session_id)
        current_target = _llm_runtime(CompactScriptedTransport("")).resolve_target(
            role=ModelRole.PRO
        )
        open_test_root_rollout_run(
            runtime_session,
            event_context=EventContext(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
            ),
            model_target=current_target.fact,
            user_input="current request",
        )
        current_start = next(
            event
            for event in runtime_session.event_log.iter(run_id=state.run_id)
            if isinstance(event, RunStartEvent)
        )
        state.run_working_set = SimpleNamespace(
            run_start_event_id=current_start.id,
            long_horizon_contract=current_start.long_horizon,
        )
        original_message_ids = [message.id for message in state.messages]
        service = _compaction_service(
            event_log=wiring.event_log,
            archive=wiring.archive,
            llm_runtime=_llm_runtime(
                CompactErrorAfterTextTransport("<summary>partial</summary>")
            ),
            runtime_session_id=runtime_session.runtime_session_id,
            runtime_session=runtime_session,
            policy=ContextCompactionPolicy(
                min_events_after_last_compact=1,
                auto_trigger_ratio=0.006,
                post_compaction_target_ratio=0.005,
                max_summary_chars=1_000,
            ),
        )
        state.run_model_target = _target(service)
        compactor = RuntimeContextCompactor(
            event_log=wiring.event_log,
            archive=wiring.archive,
            runtime_session=runtime_session,
            service=service,
        )

        model_visible_messages = [
            *rebuild_prior_messages(
                wiring.event_log,
                archive=wiring.archive,
                session_id=runtime_session.runtime_session_id,
            ),
            *state.messages,
        ]
        result = await compactor.maybe_compact_before_followup(
            state=state,
            model_visible_messages=model_visible_messages,
            protected_model_visible_messages_after=_protected_current_run_messages(
                state
            ),
        )

        assert result.compacted is False
        assert [message.id for message in state.messages] == original_message_ids
        assert any(
            isinstance(event, ContextCompactionFailedEvent) for event in result.events
        )
        emitted = await asyncio.wait_for(
            runtime_session.emit(
                typed_non_transcript_event(
                    label="after_failed_mid_turn_compaction",
                    context=EventContext(
                        run_id=state.run_id,
                        turn_id=state.turn_id,
                        reply_id=state.reply_id,
                    ),
                ),
            ),
            timeout=1,
        )
        assert (
            emitted.projection_id == "projection:test:after_failed_mid_turn_compaction"
        )

    asyncio.run(run())


def test_missing_summary_artifact_falls_back_to_full_event_replay() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old request", "old reply")
    transport = CompactScriptedTransport("<summary>Old summary.</summary>")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    event = asyncio.run(
        _compact(service, trigger="manual", reason="user_requested", force=True)
    )
    assert event is not None
    archive.blobs.pop(event.summary_artifact_id)

    messages = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    rendered = "\n".join(
        block.text
        for message in messages
        for block in message.content
        if hasattr(block, "text")
    )

    assert "<context-compaction-summary" not in rendered
    assert "old request" in rendered
    assert "old reply" in rendered


def test_auto_context_compaction_is_threshold_driven_not_run_end_unconditional() -> (
    None
):
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "tiny", "short", "ok")
    transport = CompactScriptedTransport("<summary>tiny</summary>")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    assert (
        _should_auto_compact(
            service,
        )
        is False
    )
    assert (
        asyncio.run(_compact_if_needed(service, reason="preflight_context_threshold"))
        is False
    )
    assert not any(
        isinstance(stored, ContextCompactionStartedEvent) for stored in log.iter()
    )


def test_auto_compaction_checks_threshold_before_target_feasibility() -> None:
    service, log, _transport = _contract_compaction_service(
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.80,
            post_compaction_target_ratio=0.01,
            max_summary_chars=100_000,
        )
    )
    _append_compiled_baseline(service, log, baseline_tokens=100)

    assert _should_auto_compact(service) is False
    assert (
        asyncio.run(_compact_if_needed(service, reason="preflight_context_threshold"))
        is False
    )
    assert service._consecutive_failures == 0
    assert not any(
        isinstance(event, ContextCompactionFailedEvent) for event in log.iter()
    )


def test_auto_compaction_checks_visible_threshold_before_raw_source_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, log, _transport = _contract_compaction_service()
    _append_compiled_baseline(service, log, baseline_tokens=100)

    async def fail_source_read(_self) -> list[object]:
        raise AssertionError("raw compaction source must not be read below threshold")

    monkeypatch.setattr(
        ContextCompactionService,
        "_read_bounded_source_events_async",
        fail_source_read,
    )

    assert (
        asyncio.run(
            _compact_if_needed(
                service,
                reason="preflight_context_threshold",
                model_visible_messages_before=[],
            )
        )
        is False
    )


def test_auto_context_compaction_can_compact_single_huge_completed_run() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "huge", "x" * 400_000, "y" * 400_000)
    transport = CompactScriptedTransport("<summary>Huge run summarized.</summary>")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            keep_recent_runs=3,
        ),
    )

    assert (
        _should_auto_compact(
            service,
        )
        is True
    )
    assert (
        asyncio.run(_compact_if_needed(service, reason="preflight_context_threshold"))
        is True
    )
    completed = [
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionCompletedEvent)
    ]

    assert len(completed) == 1
    assert completed[0].trigger == "auto"
    assert (
        archive.get_text(completed[0].summary_artifact_id, session_id="runtime:test")
        == "Huge run summarized."
    )


def test_auto_context_compaction_uses_model_visible_messages_not_raw_streaming_events() -> (
    None
):
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    ctx = _ctx("streamy")
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id, user_input="short"),
                user_input_chars=5,
                metadata={"user_input": "short"},
            ),
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            make_text_block_start_event(**ctx.event_fields(), block_id="text:streamy"),
            *[
                make_text_block_segment_event(
                    **ctx.event_fields(), block_id="text:streamy", delta="x"
                )
                for _ in range(500)
            ],
            make_text_block_end_event(**ctx.event_fields(), block_id="text:streamy"),
            make_thinking_block_start_event(
                **ctx.event_fields(), block_id="thinking:streamy"
            ),
            *[
                make_thinking_block_segment_event(
                    **ctx.event_fields(), block_id="thinking:streamy", delta="private"
                )
                for _ in range(500)
            ],
            make_thinking_block_end_event(
                **ctx.event_fields(), block_id="thinking:streamy"
            ),
            ReplyEndEvent(**ctx.event_fields(), model_terminal_outcome="completed"),
        ]
    )
    transport = CompactScriptedTransport("<summary>should not run</summary>")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )

    assert _should_auto_compact(service, model_visible_messages_before=[]) is False
    assert (
        asyncio.run(
            _compact_if_needed(
                service,
                reason="preflight_context_threshold",
                model_visible_messages_before=[],
            )
        )
        is False
    )
    assert not [
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionStartedEvent)
    ]


def test_auto_context_compaction_triggers_on_long_model_visible_messages() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "tiny-events", "short", "ok")
    transport = CompactScriptedTransport(
        "<summary>Visible transcript was summarized.</summary>"
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    visible = rebuild_prior_messages(log, archive=archive, session_id="runtime:test")
    visible[0].content[0].text = "visible " * 100_000

    assert _should_auto_compact(service, model_visible_messages_before=visible) is True
    assert asyncio.run(
        _compact_if_needed(
            service,
            reason="preflight_context_threshold",
            model_visible_messages_before=visible,
        )
    )


def test_auto_context_compaction_can_use_context_compiled_estimate() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(
            CompactScriptedTransport("<summary>Compiled estimate summarized.</summary>")
        ),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.006,
            post_compaction_target_ratio=0.005,
            max_summary_chars=1_000,
        ),
    )
    compiled_call = service.llm_runtime.resolve_call(
        target=_target(service),
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    ctx = _ctx("compiled-estimate")
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id, user_input="hello"),
                user_input_chars=5,
                metadata={"user_input": "hello"},
            ),
            ContextCompiledEvent(
                **ctx.event_fields(),
                **context_compiled_contract_fields(
                    estimated_tokens=1_500,
                    non_transcript_baseline_tokens=100,
                    resolved_call=compiled_call.fact,
                    context_id="context:compiled-estimate",
                ),
                context_id="context:compiled-estimate",
                model_call_index=1,
                sections=[],
                tool_specs=[],
                diagnostics=[],
                lifecycle_decisions=[],
            ),
            *_accepted_reply_events(ctx, "ok" + ("x" * 10_000)),
        ]
    )
    assert (
        _should_auto_compact(
            service,
        )
        is True
    )
    assert (
        asyncio.run(_compact_if_needed(service, reason="preflight_context_threshold"))
        is True
    )
    completed = [
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionCompletedEvent)
    ]
    assert completed[-1].target_estimate.estimate_scope == "compiled_context_baseline"
    assert completed[-1].target_estimate.non_transcript_baseline_tokens == 100


def test_auto_context_compaction_uses_recorded_compiled_baseline_without_reestimating_it() -> (
    None
):
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(
            CompactScriptedTransport("<summary>Compiled margin.</summary>")
        ),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.006,
            post_compaction_target_ratio=0.005,
            max_summary_chars=1_000,
        ),
    )
    compiled_call = service.llm_runtime.resolve_call(
        target=_target(service),
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    ctx = _ctx("compiled-margin")
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id, user_input="hello"),
                user_input_chars=5,
                metadata={"user_input": "hello"},
            ),
            ContextCompiledEvent(
                **ctx.event_fields(),
                **context_compiled_contract_fields(
                    estimated_tokens=1_500,
                    non_transcript_baseline_tokens=1_200,
                    resolved_call=compiled_call.fact,
                    context_id="context:compiled-margin",
                ),
                context_id="context:compiled-margin",
                model_call_index=1,
                sections=[],
                tool_specs=[],
                diagnostics=[],
                lifecycle_decisions=[],
            ),
            *_accepted_reply_events(ctx, "ok"),
        ]
    )
    assert (
        _should_auto_compact(
            service,
        )
        is True
    )


def test_auto_context_compaction_compiled_estimate_includes_post_model_output() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(
            CompactScriptedTransport(
                "<summary>Compiled estimate plus output.</summary>"
            )
        ),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.006,
            post_compaction_target_ratio=0.005,
            max_summary_chars=1_000,
        ),
    )
    compiled_call = service.llm_runtime.resolve_call(
        target=_target(service),
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    ctx = _ctx("compiled-post-output")
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id, user_input="hello"),
                user_input_chars=5,
                metadata={"user_input": "hello"},
            ),
            ContextCompiledEvent(
                **ctx.event_fields(),
                **context_compiled_contract_fields(
                    estimated_tokens=500,
                    non_transcript_baseline_tokens=100,
                    resolved_call=compiled_call.fact,
                    context_id="context:compiled-post-output",
                ),
                context_id="context:compiled-post-output",
                model_call_index=1,
                sections=[],
                tool_specs=[],
                diagnostics=[],
                lifecycle_decisions=[],
            ),
            *_accepted_reply_events(
                ctx, "POST_COMPILED_OUTPUT_SENTINEL " + ("x" * 10_000)
            ),
        ]
    )
    assert (
        _should_auto_compact(
            service,
        )
        is True
    )
    assert (
        asyncio.run(_compact_if_needed(service, reason="preflight_context_threshold"))
        is True
    )
    completed = [
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionCompletedEvent)
    ]
    assert completed[-1].target_estimate.estimate_scope == "compiled_context_baseline"
    assert completed[-1].target_estimate.estimated_tokens_before > 1_000


def test_compaction_input_coalesces_deltas_and_clips_large_tool_result() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    ctx = _ctx("coalesce")
    model_start, model_end, disposition = _accepted_reply_control(ctx)
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id, user_input="search news"),
                user_input_chars=11,
                metadata={"user_input": "search news"},
            ),
            ReplyStartEvent(
                id=model_start.recovery_plan.reply_start_event_id,
                **ctx.event_fields(),
                name="assistant",
            ),
            model_start,
            make_text_block_start_event(**ctx.event_fields(), block_id="text:coalesce"),
            make_text_block_segment_event(
                **ctx.event_fields(), block_id="text:coalesce", delta="hello "
            ),
            make_text_block_segment_event(
                **ctx.event_fields(), block_id="text:coalesce", delta="world"
            ),
            make_text_block_end_event(**ctx.event_fields(), block_id="text:coalesce"),
            make_tool_call_start_event(
                **ctx.event_fields(),
                tool_call_id="call:search",
                tool_call_name="terminal",
            ),
            make_tool_call_arguments_segment_event(
                **ctx.event_fields(),
                tool_call_id="call:search",
                delta='{"cmd":"search"}',
            ),
            make_tool_call_end_event(**ctx.event_fields(), tool_call_id="call:search"),
            model_end,
            ReplyEndEvent(
                id=model_start.recovery_plan.stable_reply_end_event_id,
                **ctx.event_fields(),
                model_terminal_outcome="completed",
            ),
            disposition,
            ToolResultStartEvent(
                **ctx.event_fields(),
                tool_call_id="call:search",
                tool_call_name="terminal",
            ),
            ToolResultTextDeltaEvent(
                **ctx.event_fields(), tool_call_id="call:search", delta="R" * 5_000
            ),
            ToolResultEndEvent(
                **ctx.event_fields(),
                **tool_result_end_contract_fields(
                    "call:search", tool_name="memory_search"
                ),
                tool_call_id="call:search",
                state=ToolResultState.SUCCESS,
                metadata={
                    "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
                },
                artifacts=[
                    ToolResultArtifactRef(
                        artifact_id="artifact:search:full",
                        role="output",
                        media_type="text/plain",
                        size_bytes=5000,
                    )
                ],
            ),
        ]
    )
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(CompactScriptedTransport("<summary>done</summary>")),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    plan = service._build_plan(
        log.iter(),
        compaction_id="context_compaction:00000000000000000000000000000000",
        target_model_target=_target(service),
        force=True,
    )
    assert plan is not None

    compact_input = build_compaction_input(plan)

    assert "hello world" in compact_input
    assert "artifact:search:full" in compact_input
    assert "[CLIPPED:" in compact_input
    assert "TEXT_BLOCK_DELTA" not in compact_input
    assert "TOOL_RESULT_TEXT_DELTA" not in compact_input


def test_auto_context_compaction_failure_trips_circuit_breaker_without_completed_boundary() -> (
    None
):
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "huge", "x" * 400_000, "y" * 400_000)
    transport = CompactScriptedTransport("<analysis>draft</analysis>")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            max_consecutive_failures=1,
        ),
    )

    assert (
        asyncio.run(_compact_if_needed(service, reason="preflight_context_threshold"))
        is False
    )
    assert (
        _should_auto_compact(
            service,
        )
        is False
    )
    assert (
        len(
            [
                event
                for event in log.iter()
                if isinstance(event, ContextCompactionFailedEvent)
            ]
        )
        == 1
    )
    assert not [
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionCompletedEvent)
    ]


def test_preflight_compaction_treats_empty_ledger_as_empty_source() -> None:
    log = InMemoryEventLog()
    service = _compaction_service(
        event_log=log,
        archive=InMemoryArchiveStore(),
        llm_runtime=_llm_runtime(CompactScriptedTransport("<summary>unused</summary>")),
        runtime_session_id=log.runtime_session_id,
    )

    assert _should_auto_compact(service, model_visible_messages_before=[]) is False
    assert (
        asyncio.run(
            _compact_if_needed(
                service,
                model_visible_messages_before=[],
                reason="preflight_context_threshold",
            )
        )
        is False
    )
    assert log.iter() == []


def test_preflight_current_user_input_affects_threshold_but_not_summary_input() -> None:
    log = InMemoryEventLog()
    archive = InMemoryArchiveStore()
    _append_turn(log, "old", "old", "old reply")
    transport = CompactScriptedTransport("<summary>Old summarized.</summary>")
    service = _compaction_service(
        event_log=log,
        archive=archive,
        llm_runtime=_llm_runtime(transport),
        runtime_session_id="runtime:test",
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.006,
            post_compaction_target_ratio=0.005,
            max_summary_chars=1_000,
        ),
    )

    assert (
        asyncio.run(
            _compact_if_needed(
                service,
                current_user_input_if_not_already_represented=(
                    "CURRENT_USER_INPUT_SHOULD_NOT_BE_SUMMARIZED" * 200
                ),
                reason="preflight_context_threshold",
            )
        )
        is True
    )

    compact_input = _runtime_request_text(transport.contexts[0])
    assert "CURRENT_USER_INPUT_SHOULD_NOT_BE_SUMMARIZED" not in compact_input
    completed = [
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionCompletedEvent)
    ]
    assert completed[0].reason == "preflight_context_threshold"


def test_host_session_compact_now_uses_manual_force_entrypoint(tmp_path) -> None:
    transport = CompactScriptedTransport("unused")
    runtime_wiring = build_component_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(
            agent_runtime=build_test_agent_runtime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )

    async def run() -> tuple[dict[str, object], list[dict[str, object]]]:
        try:
            return await session.compact_now(), fake.calls
        finally:
            await session.aclose()

    result, calls = asyncio.run(run())

    assert result == {
        "compacted": True,
        "compaction_id": "context_compaction:host",
        "summary_artifact_id": "context_compaction_host:summary",
        "window_id": "context_window:host",
        "through_sequence": 10,
        "keep_after_sequence": 10,
    }
    assert len(calls) == 1
    assert calls[0]["trigger"] == "manual"
    assert calls[0]["reason"] == "user_requested"
    assert calls[0]["force"] is True
    assert calls[0]["target_model_target"].fact.model_role == "pro"


def test_host_session_invokes_compaction_at_preflight_only(tmp_path) -> None:
    transport = CompactScriptedTransport("final answer")
    runtime_wiring = build_component_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(
            agent_runtime=build_test_agent_runtime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )

    async def run() -> list[dict[str, object]]:
        try:
            result = await session.run_turn("hello compaction")
            assert result.final_text == "final answer"
            return fake.calls
        finally:
            await session.aclose()

    calls = asyncio.run(run())

    preflight = next(
        call for call in calls if call.get("reason") == "preflight_context_threshold"
    )
    assert preflight["method"] == "compact_if_needed"
    assert (
        preflight["current_user_input_if_not_already_represented"] == "hello compaction"
    )
    assert preflight["model_visible_messages_before"] == []
    assert [call.get("reason") for call in calls] == ["preflight_context_threshold"]
    run_start = next(
        event
        for event in runtime_wiring.event_log.iter()
        if isinstance(event, RunStartEvent)
    )
    assert preflight["target_model_target"].fact == run_start.model_target
    assert run_start.new_run_boundary is not None
    assert (
        preflight["host_boundary_id"] == run_start.new_run_boundary.identity.boundary_id
    )
    assert preflight["host_boundary_kind"] == "pre_run"


def test_host_resolves_target_before_preflight_compaction(tmp_path) -> None:
    test_host_session_invokes_compaction_at_preflight_only(tmp_path)


def test_preflight_target_equals_run_start_target(tmp_path) -> None:
    test_host_session_invokes_compaction_at_preflight_only(tmp_path)


def test_host_session_does_not_notify_compaction_listener_after_run_end(
    tmp_path,
) -> None:
    transport = CompactScriptedTransport("final answer")
    runtime_wiring = build_component_runtime_wiring(tmp_path)
    fake = _FakeHostCompactionService()
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(
            agent_runtime=build_test_agent_runtime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )
    observed = []
    session.add_compaction_listener(observed.append)

    async def run() -> None:
        try:
            result = await session.run_turn("hello compaction")
            assert result.final_text == "final answer"
            await asyncio.sleep(0)
        finally:
            await session.aclose()

    asyncio.run(run())

    assert observed == []
    assert [call.get("reason") for call in fake.calls] == [
        "preflight_context_threshold"
    ]


def test_preflight_compaction_rebuilds_prior_messages_and_continues_original_user_input(
    tmp_path,
) -> None:
    transport = CompactScriptedTransport("final answer")
    runtime_wiring = build_component_runtime_wiring(tmp_path)
    asyncio.run(
        _emit_turn(
            runtime_wiring.runtime_session,
            "old-host",
            "old host request",
            "old host reply",
        )
    )
    fake = _FakePreflightBoundaryCompactionService(
        runtime_wiring.runtime_session,
    )
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(
            agent_runtime=build_test_agent_runtime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )

    async def run() -> None:
        try:
            result = await session.run_turn("CURRENT_USER_INPUT")
            assert result.final_text == "final answer"
        finally:
            await session.aclose()

    asyncio.run(run())

    assert (
        fake.calls[0]["current_user_input_if_not_already_represented"]
        == "CURRENT_USER_INPUT"
    )
    assert transport.contexts
    rendered = "\n".join(
        part for message in transport.contexts[0].messages for part in message.content
    )
    assert "Preflight summary sentinel." in rendered
    assert "CURRENT_USER_INPUT" in rendered
    assert "old host request" not in rendered


def test_next_run_reuses_prior_transcript_checkpoint_without_new_preflight_compaction(
    tmp_path,
) -> None:
    transport = CompactScriptedTransport("final answer")
    runtime_wiring = build_component_runtime_wiring(tmp_path)
    asyncio.run(
        _emit_turn(
            runtime_wiring.runtime_session,
            "checkpoint-seed",
            "historical request",
            "historical reply",
        )
    )
    fake = _FakeOneShotPreflightBoundaryCompactionService(
        runtime_wiring.runtime_session,
    )
    runtime_wiring = replace(runtime_wiring, compaction_service=fake)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(
            agent_runtime=build_test_agent_runtime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(transport),
            ),
            runtime_wiring=runtime_wiring,
        ),
    )

    async def run() -> None:
        try:
            first = await session.run_turn("FIRST_AFTER_CHECKPOINT")
            second = await session.run_turn("SECOND_WITHOUT_COMPACTION")
            assert first.final_text == second.final_text == "final answer"
        finally:
            await session.aclose()

    asyncio.run(run())

    events = runtime_wiring.event_log.iter()
    checkpoint = next(
        event
        for event in events
        if isinstance(event, ContextCompactionCompletedEvent)
        and event.compaction_id == "context_compaction:preflight"
    )
    second_start = next(
        event
        for event in events
        if isinstance(event, RunStartEvent)
        and event.current_user_message.text == "SECOND_WITHOUT_COMPACTION"
    )
    transcript = second_start.new_run_boundary.transcript
    assert transcript.preflight_compaction_id is None
    assert transcript.checkpoint_terminal_event_id == checkpoint.id
    assert transcript.checkpoint_terminal_sequence == checkpoint.sequence
    assert transcript.checkpoint_keep_after_sequence == checkpoint.keep_after_sequence
    compiled = next(
        event
        for event in events
        if isinstance(event, ContextCompiledEvent)
        and event.run_id == second_start.run_id
        and event.status == "compiled"
    )
    assert compiled.input_audit is not None
    assert compiled.input_audit.authority_from_sequence > 1


def test_host_session_notifies_preflight_auto_compaction_failure(tmp_path) -> None:
    runtime_wiring = build_component_runtime_wiring(tmp_path)
    fake = _FakeFailingAutoCompactionService(runtime_wiring.event_log)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(
            HostWorkspaceInput(workspace_root=tmp_path, workspace_kind="project")
        ),
        wiring=AgentRuntimeWiring(
            agent_runtime=build_test_agent_runtime(
                capability_runtime=CapabilityRuntime(),
                runtime_session=runtime_wiring.runtime_session,
                llm_runtime=_llm_runtime(CompactScriptedTransport("unused")),
            ),
            runtime_wiring=replace(runtime_wiring, compaction_service=fake),
        ),
    )
    observed = []
    session.add_compaction_listener(observed.append)

    async def run() -> None:
        try:
            await session._compact_if_needed_and_notify(
                fake, reason="preflight_context_threshold"
            )
        finally:
            await session.aclose()

    asyncio.run(run())

    assert len(observed) == 1
    assert isinstance(observed[0], ContextCompactionFailedEvent)
    assert observed[0].compaction_id == "context_compaction:failed"


def test_pending_approval_resume_does_not_auto_compact(tmp_path) -> None:
    fake = _FakeHostCompactionService()
    session, _agent, _transport, _fake = _host_interaction_fixture(
        tmp_path,
        compaction_service=fake,
        ask_permissions=True,
        replies=[
            {
                "tool_calls": [
                    {
                        "id": "call:test",
                        "name": "terminal",
                        "arguments": '{"command":"printf ok"}',
                    }
                ]
            },
            {"text": "resumed"},
        ],
    )

    async def run() -> None:
        try:
            await session.run_turn("run one command")
            pending = session.get_pending_approval()
            assert pending is not None
            fake.calls.clear()
            await session.resolve_approval(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=(
                        ToolApprovalDecision(tool_call_id="call:test", confirmed=False),
                    ),
                )
            )
        finally:
            await session.aclose()

    asyncio.run(run())

    assert fake.calls == []


def test_plan_interaction_resume_does_not_auto_compact(tmp_path) -> None:
    fake = _FakeHostCompactionService()
    session, _agent, _transport, _fake = _host_interaction_fixture(
        tmp_path,
        compaction_service=fake,
        replies=[
            {
                "tool_calls": [
                    {
                        "id": "call:plan",
                        "name": "ask_plan_question",
                        "arguments": '{"question":"choose"}',
                    }
                ]
            },
            {"text": "resumed"},
        ],
    )

    async def run() -> None:
        try:
            session.enter_plan(reason="compaction resume contract")
            await session.run_turn("ask a plan question")
            pending = session.get_pending_interaction()
            assert isinstance(pending, PendingPlanInteraction)
            fake.calls.clear()
            await session.resolve_plan_interaction(
                PlanQuestionResolution(
                    interaction_id=pending.interaction_id,
                    answer_text="A",
                )
            )
        finally:
            await session.aclose()

    asyncio.run(run())

    assert fake.calls == []


def test_mcp_input_required_resume_does_not_auto_compact(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = McpServerConfig(
        server_id="slow-docs",
        transport=McpStdioConfig(command="fake-mcp"),
        required=False,
        connect_timeout_ms=100,
        discovery_timeout_ms=100,
        startup_deadline_ms=250,
    )
    import pulsara_agent.host.core as host_core
    import pulsara_agent.host.session as host_session
    import pulsara_agent.runtime.wiring as runtime_wiring_module
    from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry

    monkeypatch.setattr(
        host_core, "load_mcp_server_configs", lambda **_kwargs: (config,)
    )
    monkeypatch.setattr(
        host_session, "load_mcp_server_configs", lambda **_kwargs: (config,)
    )
    monkeypatch.setenv(
        "PULSARA_MCP_CONTINUATION_MASTER_KEY",
        "cHVsc2FyYS10ZXN0LW1jcC1jb250aW51YXRpb24ta2V5LTAxMjM0NTY=",
    )
    monkeypatch.setenv("PULSARA_MCP_CONTINUATION_KEY_ID", "test-key")

    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()
    handler_calls = 0

    def input_required_then_complete(arguments):
        nonlocal handler_calls
        del arguments
        handler_calls += 1
        if handler_calls > 2:
            return {"status": "resumed"}
        return make_mcp_client_input_required(
            interaction_id="mcp_input_required:compaction-contract",
            server_id=config.server_id,
            request_state=f"state:compaction-contract:{handler_calls}",
            request_key="choice",
            round_ordinal=handler_calls,
        )

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await queue_ready_test_mcp_candidate(
            self,
            runtime,
            handler=input_required_then_complete,
        )
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    installed_generations: list[int] = []
    original_install_segment = RunExecutionRegistry.install_segment

    def record_install_segment(self, *args, **kwargs):
        installed = original_install_segment(self, *args, **kwargs)
        generation = getattr(installed, "segment_generation", None)
        if isinstance(generation, int):
            installed_generations.append(generation)
        return installed

    monkeypatch.setattr(
        RunExecutionRegistry,
        "install_segment",
        record_install_segment,
    )

    transport = HostInteractionTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:mcp",
                        "name": "mcp__slow-docs__lookup",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": "resumed"},
        ]
    )
    settings = PulsaraSettings(
        llm=test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api=transport.api,
        ),
        storage=compatibility_storage_config(),
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    core = component_test_host_core(settings)
    monkeypatch.setattr(
        runtime_wiring_module,
        "build_llm_runtime",
        lambda _config: LLMRuntime(config=settings.llm, registry=registry),
    )
    fake = _FakeHostCompactionService()

    async def record_auto_compaction(_service, **kwargs):
        return await fake.compact_if_needed(**kwargs)

    monkeypatch.setattr(
        ContextCompactionService,
        "compact_if_needed",
        record_auto_compaction,
    )

    async def run() -> None:
        try:
            session = await core.open_session(
                HostWorkspaceInput(
                    workspace_root=tmp_path,
                    workspace_kind="project",
                    memory_domain_id="u_test",
                ),
                host_session_id="host:mcp-compaction",
                conversation_id="conversation:mcp-compaction",
                model_role=ModelRole.FLASH,
                memory_reflection=False,
                permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
            )
            worker_release.set()
            await asyncio.wait_for(candidate_ready.wait(), timeout=0.5)
            suspended = await session.run_turn("request MCP input")
            assert isinstance(suspended, RunSuspendedOutcome)
            pending = session.get_pending_interaction()
            assert isinstance(pending, PendingMcpInputRequired)

            fake.calls.clear()
            suspended_again = await session.resolve_mcp_input_required(
                McpInputRequiredInteractionResolution(
                    interaction_id=pending.interaction_id,
                    responses={"choice": {"value": "yes"}},
                )
            )
            assert isinstance(suspended_again, RunSuspendedOutcome)
            successor = session.get_pending_interaction()
            assert isinstance(successor, PendingMcpInputRequired)
            assert successor.suspension_fact.interaction.round_count == 2

            resumed = await session.resolve_mcp_input_required(
                McpInputRequiredInteractionResolution(
                    interaction_id=successor.interaction_id,
                    responses={"choice": {"value": "yes again"}},
                )
            )
            assert resumed.status.value == "finished"
            assert handler_calls == 3
            assert fake.calls == []
            assert installed_generations == [1, 2, 3]
        finally:
            await core.shutdown()

    asyncio.run(run())


def _contract_compaction_service(
    *,
    pro_limits=None,
    flash_limits=None,
    policy: ContextCompactionPolicy | None = None,
    summary: str = "<summary>contract summary</summary>",
):
    log = InMemoryEventLog()
    _append_turn(log, "contract", "user request", "assistant reply")
    transport = CompactScriptedTransport(summary)
    service = _compaction_service(
        event_log=log,
        archive=InMemoryArchiveStore(),
        llm_runtime=_llm_runtime(
            transport,
            pro_limits=pro_limits,
            flash_limits=flash_limits,
        ),
        runtime_session_id="runtime:compaction-contract",
        policy=policy or ContextCompactionPolicy(min_events_after_last_compact=1),
    )
    return service, log, transport


def _append_compiled_baseline(
    service: ContextCompactionService,
    log: InMemoryEventLog,
    *,
    baseline_tokens: int,
    estimated_tokens: int = 400,
    label: str = "baseline",
    call=None,
) -> ContextCompiledEvent:
    if call is None:
        call = service.llm_runtime.resolve_call(
            target=_target(service),
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
    ctx = _ctx(f"compaction-{label}")
    return log.append(
        ContextCompiledEvent(
            **ctx.event_fields(),
            **context_compiled_contract_fields(
                estimated_tokens=estimated_tokens,
                tools_estimated_tokens=0,
                non_transcript_baseline_tokens=baseline_tokens,
                resolved_call=call.fact,
                context_id=f"context:{label}",
            ),
            context_id=f"context:{label}",
            model_call_index=1,
        )
    )


def test_compaction_threshold_derived_from_target_budget() -> None:
    small = test_model_limits(
        total_context_tokens=4_000,
        max_input_tokens=3_000,
        max_output_tokens=1_000,
        default_output_tokens=500,
        input_safety_margin_tokens=100,
    )
    large = test_model_limits(
        total_context_tokens=8_000,
        max_input_tokens=7_000,
        max_output_tokens=1_000,
        default_output_tokens=500,
        input_safety_margin_tokens=100,
    )
    small_service, small_log, _ = _contract_compaction_service(pro_limits=small)
    large_service, large_log, _ = _contract_compaction_service(pro_limits=large)
    small_target = _target(small_service)
    large_target = _target(large_service)
    small_plan = small_service._build_plan(
        small_log.iter(),
        compaction_id="context_compaction:small",
        target_model_target=small_target,
        force=True,
    )
    large_plan = large_service._build_plan(
        large_log.iter(),
        compaction_id="context_compaction:large",
        target_model_target=large_target,
        force=True,
    )
    assert small_plan is not None and large_plan is not None
    assert small_plan.threshold_tokens == int(
        small_target.context_budget.input_budget_tokens * 0.80
    )
    assert large_plan.threshold_tokens > small_plan.threshold_tokens


def test_compaction_post_target_derived_from_target_budget() -> None:
    service, log, _ = _contract_compaction_service()
    target = _target(service)
    plan = service._build_plan(
        log.iter(),
        compaction_id="context_compaction:post-target",
        target_model_target=target,
        force=True,
    )
    assert plan is not None
    assert plan.post_compaction_target_tokens == int(
        target.context_budget.input_budget_tokens * 0.55
    )


def test_compaction_without_baseline_marks_transcript_only() -> None:
    service, log, _ = _contract_compaction_service()
    plan = service._build_plan(
        log.iter(),
        compaction_id="context_compaction:transcript-only",
        target_model_target=_target(service),
        force=True,
    )
    assert plan is not None
    assert plan.target_estimate.estimate_scope == "transcript_only"
    assert plan.target_estimate.non_transcript_baseline_tokens is None


def test_compaction_prefers_matching_latest_compiled_baseline() -> None:
    service, log, _ = _contract_compaction_service()
    matching = _append_compiled_baseline(
        service,
        log,
        baseline_tokens=111,
        label="matching",
    )
    other_runtime = _llm_runtime(
        CompactScriptedTransport("unused"),
        pro_limits=test_model_limits(
            total_context_tokens=8_192,
            max_input_tokens=7_000,
            max_output_tokens=1_024,
            default_output_tokens=512,
            input_safety_margin_tokens=128,
        ),
    )
    other_call = other_runtime.resolve_call(
        target=other_runtime.resolve_target(role=ModelRole.PRO),
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    _append_compiled_baseline(
        service,
        log,
        baseline_tokens=999,
        estimated_tokens=1_200,
        label="later-mismatch",
        call=other_call,
    )

    plan = service._build_plan(
        log.iter(),
        compaction_id="context_compaction:matching-baseline",
        target_model_target=_target(service),
        force=True,
    )

    assert plan is not None
    assert plan.target_estimate.estimate_scope == "compiled_context_baseline"
    assert plan.target_estimate.basis_context_id == matching.context_id
    assert plan.target_estimate.basis_context_compiled_sequence == matching.sequence
    assert plan.target_estimate.non_transcript_baseline_tokens == 111


def test_compaction_full_estimate_preserves_non_transcript_baseline() -> None:
    service, log, _ = _contract_compaction_service(
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            max_summary_chars=512,
        )
    )
    _append_compiled_baseline(service, log, baseline_tokens=75)
    plan = service._build_plan(
        log.iter(),
        compaction_id="context_compaction:full-estimate",
        target_model_target=_target(service),
        force=True,
    )
    assert plan is not None
    estimate = plan.target_estimate
    assert estimate.estimated_tokens_before == (75 + estimate.transcript_tokens_before)


def test_mid_turn_protected_messages_are_counted_after_but_not_summarized() -> None:
    service, _log, transport = _contract_compaction_service()
    protected_raw = UserMsg(
        name="user",
        id="user-message:protected-run",
        content="PROTECTED_CURRENT_RUN_SENTINEL " + ("x" * 20_000),
        metadata={"run_id": "run:protected"},
    )
    protected = LLMMessage.user("PROTECTED_CURRENT_RUN_SENTINEL " + ("x" * 20_000))

    completed = asyncio.run(
        _compact(
            service,
            trigger="manual",
            reason="mid-turn-protected-accounting",
            force=True,
            model_visible_messages_before=[protected_raw],
            protected_model_visible_messages_after=(protected,),
            current_user_input_if_not_already_represented="",
        )
    )

    assert completed is not None
    estimate = completed.target_estimate
    assert estimate.protected_transcript_tokens > 5_000
    assert estimate.summary_tokens_actual is not None
    assert estimate.transcript_tokens_after == (
        estimate.protected_transcript_tokens + estimate.summary_tokens_actual
    )
    compact_input = "\n".join(
        part for message in transport.contexts[0].messages for part in message.content
    )
    assert "PROTECTED_CURRENT_RUN_SENTINEL" not in compact_input


def test_compiled_baseline_estimate_requires_complete_attribution() -> None:
    target = _target(_contract_compaction_service()[0]).fact
    with pytest.raises(ValidationError, match="compiled baseline attribution"):
        CompactionTargetEstimateFact(
            estimate_scope="compiled_context_baseline",
            basis_context_id="context:basis",
            basis_context_compiled_sequence=None,
            target_fingerprint=target.target_fingerprint,
            non_transcript_baseline_tokens=75,
            transcript_tokens_before=225,
            estimated_tokens_before=300,
            summary_tokens_reserved=50,
            retained_transcript_tokens=0,
            protected_transcript_tokens=0,
            summary_tokens_actual=None,
            transcript_tokens_after=None,
            estimated_tokens_after=None,
            predicted_post_target_reached=None,
        )


def test_compiled_baseline_actual_after_requires_prediction() -> None:
    target = _target(_contract_compaction_service()[0]).fact
    with pytest.raises(ValidationError, match="requires a target prediction"):
        CompactionTargetEstimateFact(
            estimate_scope="compiled_context_baseline",
            basis_context_id="context:basis",
            basis_context_compiled_sequence=7,
            target_fingerprint=target.target_fingerprint,
            non_transcript_baseline_tokens=75,
            transcript_tokens_before=225,
            estimated_tokens_before=300,
            summary_tokens_reserved=50,
            retained_transcript_tokens=70,
            protected_transcript_tokens=10,
            summary_tokens_actual=20,
            transcript_tokens_after=100,
            estimated_tokens_after=175,
            predicted_post_target_reached=None,
        )


def test_compaction_actual_after_requires_complete_transcript_formula() -> None:
    target = _target(_contract_compaction_service()[0]).fact
    with pytest.raises(ValidationError, match=r"summary \+ retained \+ protected"):
        CompactionTargetEstimateFact(
            estimate_scope="transcript_only",
            basis_context_id=None,
            target_fingerprint=target.target_fingerprint,
            non_transcript_baseline_tokens=None,
            transcript_tokens_before=300,
            estimated_tokens_before=300,
            summary_tokens_reserved=50,
            retained_transcript_tokens=0,
            protected_transcript_tokens=100,
            summary_tokens_actual=10,
            transcript_tokens_after=1,
            estimated_tokens_after=1,
            predicted_post_target_reached=None,
        )


def test_compaction_observed_failure_requires_complete_transcript_formula() -> None:
    with pytest.raises(ValidationError, match=r"summary \+ retained \+ protected"):
        CompactionObservedAfterMeasurementFact(
            summary_tokens_actual=10,
            retained_transcript_tokens=0,
            protected_transcript_tokens=100,
            transcript_tokens_after=1,
            estimated_tokens_after=1,
            predicted_post_target_reached=None,
            violation_code="summary_tokens_exceed_reservation",
        )


def test_compaction_completed_prediction_must_match_post_target() -> None:
    fields = compaction_completed_contract_fields(
        estimated_tokens_before=300,
        estimated_tokens_after=100,
    )
    target = fields["target_model_target"]
    fields["target_estimate"] = CompactionTargetEstimateFact(
        estimate_scope="compiled_context_baseline",
        basis_context_id="context:basis",
        basis_context_compiled_sequence=7,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=75,
        transcript_tokens_before=225,
        estimated_tokens_before=300,
        summary_tokens_reserved=50,
        retained_transcript_tokens=70,
        protected_transcript_tokens=10,
        summary_tokens_actual=20,
        transcript_tokens_after=100,
        estimated_tokens_after=175,
        predicted_post_target_reached=True,
    )
    fields["predicted_post_target_reached"] = True
    fields["post_compaction_target_tokens"] = 100
    ctx = _ctx("compaction:invalid-prediction")
    with pytest.raises(ValidationError, match="does not match the post-compaction"):
        ContextCompactionCompletedEvent(
            **ctx.event_fields(),
            **fields,
            compaction_id="context_compaction:invalid-prediction",
            trigger="manual",
            reason="contract",
            window_number=1,
            window_id="context_window:invalid-prediction",
            summary_artifact_id="artifact:summary",
            summary_chars=12,
            threshold_tokens=200,
            through_sequence=10,
            keep_after_sequence=10,
        )


def _failed_event_after_summary(
    *,
    failure_stage: str,
    target_estimate: CompactionTargetEstimateFact | None = None,
    observed_after_measurement: CompactionObservedAfterMeasurementFact | None = None,
    post_compaction_target_tokens: int | None = None,
) -> ContextCompactionFailedEvent:
    fields = compaction_started_contract_fields(estimated_tokens_before=300)
    summarizer_call = fields["summarizer_call"]
    return ContextCompactionFailedEvent(
        **_ctx(f"compaction:failed:{failure_stage}").event_fields(),
        compaction_id=f"context_compaction:failed:{failure_stage}",
        trigger="manual",
        reason="contract",
        window_number=1,
        window_id=f"context_window:failed:{failure_stage}",
        target_model_target=fields["target_model_target"],
        target_input_budget_tokens=fields["target_input_budget_tokens"],
        threshold_tokens=200,
        post_compaction_target_tokens=(
            post_compaction_target_tokens
            if post_compaction_target_tokens is not None
            else fields["post_compaction_target_tokens"]
        ),
        failure_stage=failure_stage,
        target_estimate=target_estimate or fields["target_estimate"],
        observed_after_measurement=observed_after_measurement,
        summarizer_target=summarizer_call.target,
        summarizer_call=summarizer_call,
        summarizer_context_id=fields["summarizer_context_id"],
        summarizer_input_estimated_tokens=fields["summarizer_input_estimated_tokens"],
        summarizer_input_budget_tokens=fields["summarizer_input_budget_tokens"],
        summarizer_usage_status="missing",
        summarizer_usage=None,
        summarizer_estimated_input_tokens=fields["summarizer_input_estimated_tokens"],
        summarizer_reported_model_id=None,
        through_sequence=10,
        keep_after_sequence=10,
        error_type="RuntimeError",
        message="synthetic failure",
        started_event_id="context_compaction_started:synthetic",
        termination_kind="failed",
    )


@pytest.mark.parametrize("failure_stage", ["artifact_write", "completed_append"])
def test_post_summary_persistence_failure_requires_actual_after_measurements(
    failure_stage: str,
) -> None:
    with pytest.raises(ValidationError, match="requires actual after measurements"):
        _failed_event_after_summary(failure_stage=failure_stage)


def test_failed_event_prediction_must_match_post_target() -> None:
    fields = compaction_started_contract_fields(estimated_tokens_before=300)
    target = fields["target_model_target"]
    estimate = CompactionTargetEstimateFact(
        estimate_scope="compiled_context_baseline",
        basis_context_id="context:basis",
        basis_context_compiled_sequence=7,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=75,
        transcript_tokens_before=225,
        estimated_tokens_before=300,
        summary_tokens_reserved=50,
        retained_transcript_tokens=70,
        protected_transcript_tokens=10,
        summary_tokens_actual=20,
        transcript_tokens_after=100,
        estimated_tokens_after=175,
        predicted_post_target_reached=True,
    )
    with pytest.raises(ValidationError, match="does not match the post-compaction"):
        _failed_event_after_summary(
            failure_stage="artifact_write",
            target_estimate=estimate,
            post_compaction_target_tokens=100,
        )


def test_transcript_only_failed_event_keeps_prediction_none() -> None:
    completed_fields = compaction_completed_contract_fields(
        estimated_tokens_before=300,
        estimated_tokens_after=100,
    )
    event = _failed_event_after_summary(
        failure_stage="artifact_write",
        target_estimate=completed_fields["target_estimate"],
        post_compaction_target_tokens=50,
    )
    assert event.target_estimate is not None
    assert event.target_estimate.predicted_post_target_reached is None


def test_failed_event_rejects_two_competing_actual_measurements() -> None:
    completed_fields = compaction_completed_contract_fields(
        estimated_tokens_before=300,
        estimated_tokens_after=100,
    )
    target_estimate = completed_fields["target_estimate"]
    assert isinstance(target_estimate, CompactionTargetEstimateFact)
    observed_summary_tokens = target_estimate.summary_tokens_reserved + 1
    observed = CompactionObservedAfterMeasurementFact(
        summary_tokens_actual=observed_summary_tokens,
        retained_transcript_tokens=target_estimate.retained_transcript_tokens,
        protected_transcript_tokens=target_estimate.protected_transcript_tokens,
        transcript_tokens_after=(
            observed_summary_tokens
            + target_estimate.retained_transcript_tokens
            + target_estimate.protected_transcript_tokens
        ),
        estimated_tokens_after=(
            observed_summary_tokens
            + target_estimate.retained_transcript_tokens
            + target_estimate.protected_transcript_tokens
        ),
        predicted_post_target_reached=None,
        violation_code="summary_tokens_exceed_reservation",
    )
    with pytest.raises(ValidationError, match="planning-only target estimate"):
        _failed_event_after_summary(
            failure_stage="summary_validation",
            target_estimate=target_estimate,
            observed_after_measurement=observed,
        )


def test_compaction_predicted_post_target_includes_compiled_non_transcript_baseline() -> (
    None
):
    service, log, _ = _contract_compaction_service(
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.90,
            post_compaction_target_ratio=0.70,
            max_summary_chars=512,
        )
    )
    _append_compiled_baseline(service, log, baseline_tokens=75)

    completed = asyncio.run(
        _compact(service, trigger="manual", reason="baseline", force=True)
    )

    assert completed is not None
    estimate = completed.target_estimate
    assert estimate.estimated_tokens_after == (75 + estimate.transcript_tokens_after)
    assert completed.predicted_post_target_reached is True


def test_compaction_compiled_baseline_is_prediction_not_next_compile_truth() -> None:
    service, log, _ = _contract_compaction_service(
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.90,
            post_compaction_target_ratio=0.70,
            max_summary_chars=512,
        )
    )
    basis = _append_compiled_baseline(service, log, baseline_tokens=75)
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="prediction", force=True)
    )
    assert completed is not None
    assert completed.target_estimate.basis_context_id == basis.context_id
    assert completed.predicted_post_target_reached is True
    # The event deliberately records a prediction tied to the old compile; it
    # does not claim that a future context was compiled with the same sources.
    assert completed.target_estimate.estimate_scope == "compiled_context_baseline"


def test_compaction_transcript_only_never_claims_predicted_post_target_reached() -> (
    None
):
    service, _log, _ = _contract_compaction_service()
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="contract", force=True)
    )
    assert completed is not None
    assert completed.target_estimate.estimate_scope == "transcript_only"
    assert completed.predicted_post_target_reached is None


def test_manual_compaction_uses_direct_summarizer_call_without_main_call() -> None:
    service, _log, _ = _contract_compaction_service()
    target = _target(service)
    model_lifecycle_owner = getattr(
        service.runtime_session,
        "_backing_runtime_session",
        service.runtime_session,
    )
    model_lifecycle_log = model_lifecycle_owner.event_log
    existing_start_ids = {
        event.id
        for event in model_lifecycle_log.iter()
        if isinstance(event, ModelCallStartEvent)
    }
    attempt = asyncio.run(
        service.compact(
            target_model_target=target,
            trigger="manual",
            reason="contract",
            force=True,
        )
    )
    completed = attempt.terminal_event
    assert isinstance(completed, ContextCompactionCompletedEvent)
    assert completed is not None
    assert completed.target_model_target == target.fact
    assert (
        completed.summarizer_call.purpose == ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    new_starts = [
        event
        for event in model_lifecycle_log.iter()
        if isinstance(event, ModelCallStartEvent) and event.id not in existing_start_ids
    ]
    assert len(new_starts) == 1
    assert new_starts[0].recovery_plan.lifecycle_kind == "direct_internal_call"


def test_preflight_compaction_uses_pending_run_target(tmp_path) -> None:
    test_preflight_target_equals_run_start_target(tmp_path)


def test_mid_turn_compaction_uses_current_call_target(tmp_path) -> None:
    from tests.test_agent_runtime_loop import (
        test_agent_runtime_runs_context_compactor_before_tool_followup,
    )

    test_agent_runtime_runs_context_compactor_before_tool_followup(tmp_path)


def test_compaction_summarizer_has_separate_call() -> None:
    service, _log, _ = _contract_compaction_service()
    target = _target(service)
    attempt = asyncio.run(
        service.compact(
            target_model_target=target,
            trigger="manual",
            reason="contract",
            force=True,
        )
    )
    completed = attempt.terminal_event
    assert isinstance(completed, ContextCompactionCompletedEvent)
    assert completed is not None
    assert completed.summarizer_call.context_mode == "direct"
    assert completed.summarizer_call.target.model_role == "flash"
    assert completed.target_model_target.model_role == "pro"


def test_compaction_summarizer_input_fits_flash_budget() -> None:
    service, _log, transport = _contract_compaction_service()
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="fits", force=True)
    )
    assert completed is not None
    assert completed.summarizer_input_estimated_tokens <= (
        completed.summarizer_input_budget_tokens
    )
    assert len(transport.contexts) == 1


def test_compaction_summarizer_input_uses_metadata_only_degradation(
    monkeypatch,
) -> None:
    flash = test_model_limits(
        total_context_tokens=2_400,
        max_input_tokens=2_200,
        max_output_tokens=200,
        default_output_tokens=100,
        input_safety_margin_tokens=50,
    )
    service, _log, transport = _contract_compaction_service(flash_limits=flash)
    monkeypatch.setattr(
        compaction_service_module,
        "build_compaction_input",
        lambda _plan: "x" * 20_000,
    )
    monkeypatch.setattr(
        compaction_service_module,
        "build_metadata_only_compaction_input",
        lambda _plan: "METADATA_ONLY_SENTINEL",
    )

    completed = asyncio.run(
        _compact(service, trigger="manual", reason="degrade", force=True)
    )

    assert completed is not None
    assert len(transport.contexts) == 1
    assert "METADATA_ONLY_SENTINEL" in "\n".join(
        transport.contexts[0].messages[-1].content
    )


def test_compaction_summary_output_uses_flash_default() -> None:
    service, _log, _transport = _contract_compaction_service()
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="default-output", force=True)
    )
    assert completed is not None
    target = completed.summarizer_call.target
    assert (
        target.context_budget.effective_output_tokens
        == target.limits.default_output_tokens
    )


def test_compaction_planner_uses_summary_token_reservation_not_future_actual_summary() -> (
    None
):
    service, log, _ = _contract_compaction_service(
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            max_summary_chars=512,
        )
    )
    plan = service._build_plan(
        log.iter(),
        compaction_id="context_compaction:reservation",
        target_model_target=_target(service),
        force=True,
    )
    assert plan is not None
    estimate = plan.target_estimate
    assert estimate.summary_tokens_reserved > 0
    assert estimate.summary_tokens_actual is None
    assert estimate.estimated_tokens_after is None
    assert estimate.predicted_post_target_reached is None


def test_compaction_started_records_reservation_but_no_actual_after_measurement() -> (
    None
):
    service, log, _ = _contract_compaction_service()
    asyncio.run(_compact(service, trigger="manual", reason="started", force=True))
    started = next(
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionStartedEvent)
    )
    assert started.target_estimate.summary_tokens_reserved > 0
    assert started.target_estimate.summary_tokens_actual is None
    assert started.target_estimate.estimated_tokens_after is None
    assert started.target_estimate.predicted_post_target_reached is None


def test_compaction_completed_uses_actual_summary_replay_estimate() -> None:
    service, _log, _ = _contract_compaction_service()
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="actual-summary", force=True)
    )
    assert completed is not None
    estimate = completed.target_estimate
    assert estimate.summary_tokens_actual is not None
    assert estimate.transcript_tokens_after is not None
    assert estimate.estimated_tokens_after is not None


def test_compaction_summary_actual_must_not_exceed_reservation(monkeypatch) -> None:
    service, log, _ = _contract_compaction_service(
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            max_summary_chars=512,
        )
    )
    calls = 0

    def inconsistent_estimate(**_kwargs):
        nonlocal calls
        calls += 1
        return 1 if calls == 1 else 100

    monkeypatch.setattr(
        compaction_service_module,
        "estimate_compaction_summary_replay_tokens",
        inconsistent_estimate,
    )

    with pytest.raises(ValueError, match="actual summary tokens exceed"):
        asyncio.run(
            _compact(service, trigger="manual", reason="bad-reservation", force=True)
        )
    assert not any(
        isinstance(event, ContextCompactionCompletedEvent) for event in log.iter()
    )
    failed = next(
        event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)
    )
    assert failed.failure_stage == "summary_validation"
    assert failed.target_estimate is not None
    assert failed.target_estimate.summary_tokens_actual is None
    assert failed.observed_after_measurement is not None
    assert failed.observed_after_measurement.summary_tokens_actual == 100
    assert (
        failed.observed_after_measurement.violation_code
        == "summary_tokens_exceed_reservation"
    )
    assert failed.observed_after_measurement.transcript_tokens_after >= 100
    assert failed.observed_after_measurement.estimated_tokens_after >= 100


def test_compaction_predicted_post_target_is_decided_only_after_summary_generation() -> (
    None
):
    service, log, _ = _contract_compaction_service()
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="prediction-stage", force=True)
    )
    started = next(
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionStartedEvent)
    )
    assert completed is not None
    assert started.target_estimate.predicted_post_target_reached is None
    assert completed.target_estimate.summary_tokens_actual is not None


def test_compaction_target_unreachable_is_explicit() -> None:
    service, log, _ = _contract_compaction_service(
        policy=ContextCompactionPolicy(
            min_events_after_last_compact=1,
            auto_trigger_ratio=0.80,
            post_compaction_target_ratio=0.05,
            max_summary_chars=100_000,
        )
    )
    _append_compiled_baseline(service, log, baseline_tokens=100)
    with pytest.raises(CompactionTargetUnreachable):
        service._build_plan(
            log.iter(),
            compaction_id="context_compaction:unreachable",
            target_model_target=_target(service),
            force=True,
        )


def test_compaction_planning_failure_allows_missing_summarizer_call(
    monkeypatch,
) -> None:
    service, log, _ = _contract_compaction_service()

    def fail_plan(*_args, **_kwargs):
        raise RuntimeError("planning failed")

    monkeypatch.setattr(ContextCompactionService, "_build_plan", fail_plan)
    with pytest.raises(RuntimeError, match="planning failed"):
        asyncio.run(
            _compact(service, trigger="manual", reason="planning-failure", force=True)
        )
    failed = next(
        event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)
    )
    assert failed.failure_stage == "planning"
    assert failed.summarizer_call is None


def test_compaction_resolution_failure_allows_missing_summarizer_call(
    monkeypatch,
) -> None:
    service, log, _ = _contract_compaction_service()
    resolve_target = service.llm_runtime.resolve_target

    def fail_resolution(**kwargs):
        if kwargs.get("role") is ModelRole.FLASH:
            raise ModelOptionUnsupported("unsupported summarizer option")
        return resolve_target(**kwargs)

    monkeypatch.setattr(service.llm_runtime, "resolve_target", fail_resolution)
    with pytest.raises(ModelOptionUnsupported):
        asyncio.run(
            _compact(service, trigger="manual", reason="resolution-failure", force=True)
        )
    failed = next(
        event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)
    )
    assert failed.failure_stage == "summarizer_resolution"
    assert failed.summarizer_call is None


def test_compaction_input_build_failure_requires_summarizer_call() -> None:
    tiny_flash = test_model_limits(
        total_context_tokens=32,
        max_input_tokens=24,
        max_output_tokens=8,
        default_output_tokens=8,
        input_safety_margin_tokens=4,
    )
    service, log, _ = _contract_compaction_service(flash_limits=tiny_flash)
    with pytest.raises(CompactionSummarizerInputBudgetExceeded):
        asyncio.run(
            _compact(service, trigger="manual", reason="input-build", force=True)
        )
    failed = next(
        event for event in log.iter() if isinstance(event, ContextCompactionFailedEvent)
    )
    assert failed.failure_stage == "summarizer_input_build"
    assert failed.summarizer_call is not None


def test_compaction_started_requires_built_context_and_input_estimate() -> None:
    service, log, _ = _contract_compaction_service()
    asyncio.run(
        _compact(service, trigger="manual", reason="started-contract", force=True)
    )
    started = next(
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionStartedEvent)
    )
    assert started.summarizer_context_id
    assert started.summarizer_input_estimated_tokens > 0
    assert (
        started.summarizer_input_estimated_tokens
        <= started.summarizer_input_budget_tokens
    )


def test_compaction_summarizer_input_over_budget_fails_before_provider() -> None:
    tiny_flash = test_model_limits(
        total_context_tokens=32,
        max_input_tokens=24,
        max_output_tokens=8,
        default_output_tokens=8,
        input_safety_margin_tokens=4,
    )
    service, _log, transport = _contract_compaction_service(
        flash_limits=tiny_flash,
    )
    with pytest.raises(CompactionSummarizerInputBudgetExceeded):
        asyncio.run(_compact(service, trigger="manual", reason="contract", force=True))
    assert transport.contexts == []


def test_compaction_summary_output_cap_comes_from_flash_slot() -> None:
    flash = test_model_limits(
        total_context_tokens=4_000,
        max_input_tokens=3_500,
        max_output_tokens=500,
        default_output_tokens=200,
        input_safety_margin_tokens=100,
    )
    policy = ContextCompactionPolicy(
        min_events_after_last_compact=1,
        summarizer_options=LLMOptions(),
    )
    service, _log, _transport = _contract_compaction_service(
        flash_limits=flash,
        policy=policy,
    )
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="contract", force=True)
    )
    assert completed is not None
    assert (
        completed.summarizer_call.target.context_budget.effective_output_tokens == 200
    )


def test_compaction_events_record_both_contracts() -> None:
    service, log, _ = _contract_compaction_service()
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="contract", force=True)
    )
    started = next(
        event
        for event in log.iter()
        if isinstance(event, ContextCompactionStartedEvent)
    )
    assert completed is not None
    assert started.target_model_target == completed.target_model_target
    assert started.summarizer_call == completed.summarizer_call
    assert started.summarizer_input_estimated_tokens == (
        completed.summarizer_estimated_input_tokens
    )


def test_compaction_terminal_event_preserves_usage_or_missing_status() -> None:
    service, _log, _ = _contract_compaction_service()
    completed = asyncio.run(
        _compact(service, trigger="manual", reason="contract", force=True)
    )
    assert completed is not None
    assert completed.summarizer_usage_status == "missing"
    assert completed.summarizer_usage is None
    assert completed.summarizer_estimated_input_tokens > 0
    assert (
        completed.summarizer_reported_model_id
        == completed.summarizer_call.target.model_id
    )


async def _resume_contract_fixture(tmp_path):
    session, agent, transport, _fake = _host_interaction_fixture(
        tmp_path,
        ask_permissions=True,
        replies=[
            {
                "tool_calls": [
                    {
                        "id": "call:model-contract",
                        "name": "terminal",
                        "arguments": '{"command":"printf ok"}',
                    }
                ]
            },
            {"text": "resumed"},
        ],
    )
    suspended = await session.run_turn("freeze the model target")
    pending = session.get_pending_approval()
    assert pending is not None
    assert isinstance(suspended, RunSuspendedOutcome)
    return session, agent, transport, suspended.owner_identity.run_id, pending


def test_resume_rebinds_original_run_target(tmp_path) -> None:
    async def run() -> None:
        session, _agent, transport, run_id, pending = await _resume_contract_fixture(
            tmp_path
        )
        try:
            durable_target = next(
                event.model_target
                for event in session.wiring.runtime_wiring.event_log.iter()
                if isinstance(event, RunStartEvent) and event.run_id == run_id
            )
            await session.resolve_approval(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=(
                        ToolApprovalDecision(
                            tool_call_id="call:model-contract",
                            confirmed=False,
                        ),
                    ),
                )
            )
            assert transport.contexts[-1].target_fingerprint == (
                durable_target.target_fingerprint
            )
        finally:
            await session.aclose()

    asyncio.run(run())


def test_resume_rejects_changed_model_target(tmp_path) -> None:
    async def run() -> None:
        session, agent, _transport, _run_id, pending = await _resume_contract_fixture(
            tmp_path
        )
        agent.llm_runtime._config = replace(
            agent.llm_runtime._config,
            pro=test_model_slot("changed-pro-model"),
        )
        try:
            with pytest.raises(ModelTargetBindingMismatch):
                await session.resolve_approval(
                    ApprovalResolution(
                        approval_id=pending.approval_id,
                        decisions=(
                            ToolApprovalDecision(
                                tool_call_id="call:model-contract",
                                confirmed=False,
                            ),
                        ),
                    )
                )
        finally:
            await session.aclose()

    asyncio.run(run())


def test_resume_does_not_use_current_config_as_fallback(tmp_path) -> None:
    test_resume_rejects_changed_model_target(tmp_path)
