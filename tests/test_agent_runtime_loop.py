import asyncio
import json
import threading
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from tests.conftest import (
    run_end_contract_fields,
    run_start_permission_fields,
    tool_result_end_contract_fields,
)
from tests.support.runtime_session import in_memory_runtime_session

from tests.support.raw_provider import (
    RawProviderTextBlockEnd,
    RawProviderTextBlockStart,
    RawProviderTextDelta,
    RawProviderToolCallDelta,
    RawProviderToolCallEnd,
    RawProviderToolCallStart,
)

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.event import (
    AgentEvent,
    CapabilityGateDecisionEvent,
    ContextCompiledEvent,
    ContextProjectionRewritePageEvent,
    ContextWindowClosedEvent,
    EventContext,
    EventType,
    McpCapabilitySnapshotInstalledEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ModelCallRejectedEvent,
    ProviderModelStreamErrorEvent,
    PhysicalOperationReservationCreatedEvent,
    PhysicalOperationReservationSettledEvent,
    PhysicalOperationReservationSuspendedEvent,
    RequireUserConfirmEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RolloutBudgetAccountClosedEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    ToolExecutionSuspendedEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.capability import (
    LocalSkillCapabilityProvider,
    LocalSkillProvider,
)
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.capability.descriptor import (
    CapabilityDescriptor,
    CapabilityProviderKind,
)
from pulsara_agent.capability.builtin_provider import builtin_tool_descriptors
from pulsara_agent.capability.provider import (
    CapabilityDescriptorSnapshotOutput,
)
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.result_contracts import generic_result_render_contract
from pulsara_agent.capability.result_semantics import (
    FrozenToolResultSemanticsRuntimeInput,
    build_terminal_payload_timing,
)
from pulsara_agent.llm import LLMRuntime
from pulsara_agent.llm.errors import ModelContextIdentityMismatch
from tests.support import run_agent_task, stream_agent_task, test_llm_config
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.primitives.mcp import (
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpServerLifecycleTimingFact,
)
from pulsara_agent.primitives.capability import CapabilityExecutionSurfaceIdentityFact
from pulsara_agent.primitives.long_horizon import (
    LongHorizonActionClass,
    RolloutBudgetBucket,
    RolloutPhase,
    RolloutReservationFact,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.memory.recall.service import RecallResult, RecallStatus
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.memory import (
    ExecutionEvidenceLedger,
    ExecutionEvidencePersistenceHook,
    InMemoryArchiveStore,
)
from pulsara_agent.message import (
    AssistantMsg,
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.message.reducer import MessageReducer
from pulsara_agent.runtime import (
    ApprovalResolution,
    AgentRuntime,
    EventBatchCommitOutcome,
    EventCommitError,
    EventPublicationAfterCommitError,
    EventWriteCancelled,
    InRunRecoveryCause,
    LoopBudget,
    LoopState,
    LoopStatus,
    LoopTransition,
    ToolApprovalDecision,
    build_tool_result_error_events,
)
from pulsara_agent.runtime.context_input.replay import load_context_input_manifest
from pulsara_agent.runtime.compaction.inline import MidTurnCompactionResult
from pulsara_agent.runtime.execution_handles import BoundaryExecutionHandles
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
from pulsara_agent.runtime.permission import (
    EffectivePermissionPolicy,
    PermissionDecision,
    PermissionDecisionKind,
    preset_to_policy,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.tool_result import (
    TerminalCommandErrorEssentialFact,
    TerminalCommandDomainSubmissionFact,
    ToolResultEssentialCapturePolicyFact,
    ToolResultRenderVariantCode,
)
from pulsara_agent.runtime.run_entry import RunWorkingSet
from pulsara_agent.runtime.terminal import TerminalStatus
from pulsara_agent.runtime.hooks import NoopMemoryHooks
from pulsara_agent.runtime.mcp.types import McpPendingInstallationAudit
from pulsara_agent.runtime.plan import McpInputRequiredInteractionResolution
from pulsara_agent.runtime.tool_artifacts import ToolResultArtifactRecord
from pulsara_agent.runtime.tool_loop import _tool_result_from_event_slice
from pulsara_agent.runtime.tool_action import fixed_tool_action_policy
from pulsara_agent.runtime.tool_execution import (
    RuntimeSessionToolExecutionEventCommitPort,
    build_tool_result_terminal_event,
)
from pulsara_agent.runtime.long_horizon.run_contract import (
    empty_projection_state_fingerprint,
    prepare_root_long_horizon_run,
)
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.tools.base import (
    ToolCall,
    ToolExecutionResult,
    ToolExecutionSuspended,
    ToolRuntimeContext,
)
from pulsara_agent.tools.registry import ToolRegistry
from pulsara_agent.tools.builtins.memory_query import MemorySearchTool


def _without_context_timing_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("[context timing:")
    )


class ScriptedTransport:
    api = "scripted"
    binding_id = "test.scripted"
    contract_version = "v1"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []
        self.calls = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append(call)
        self.contexts.append(context)
        reply = self.replies.pop(0)
        if "text" in reply:
            yield RawProviderTextBlockStart(
                **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
            )
            yield RawProviderTextDelta(
                **event_context.event_fields(),
                block_id=f"text:{len(self.contexts)}",
                delta=reply["text"],
            )
            yield RawProviderTextBlockEnd(
                **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
            )
        for call in reply.get("tool_calls", []):
            yield RawProviderToolCallStart(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield RawProviderToolCallDelta(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                delta=call["arguments"],
            )
            yield RawProviderToolCallEnd(
                **event_context.event_fields(), tool_call_id=call["id"]
            )


class RecordingContextCompactor:
    def __init__(self) -> None:
        self.calls: list[tuple[LoopTransition, int, int]] = []

    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
        protected_model_visible_messages_after,
    ):
        self.calls.append(
            (
                state.last_transition,
                len(state.pending_tool_calls),
                len(model_visible_messages),
            )
        )
        assert protected_model_visible_messages_after
        return MidTurnCompactionResult(compacted=False, skipped_reason="test")


def make_llm_runtime(transport: ScriptedTransport) -> LLMRuntime:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="scripted",
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(config=config, registry=registry)


async def _collect_async(stream) -> list[AgentEvent]:
    return [event async for event in stream]


def _terminal_ask_policy() -> EffectivePermissionPolicy:
    return preset_to_policy(PermissionMode.ASK_PERMISSIONS)


def _terminal_bypass_policy() -> EffectivePermissionPolicy:
    return preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)


def test_loop_state_initializes_from_runtime_session(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    state = LoopState(session_id=runtime_session.runtime_session_id)
    first_turn = state.turn_id
    state.transition(LoopTransition.CONTINUE_AFTER_MODEL)
    state.begin_next_turn()

    assert state.session_id == runtime_session.runtime_session_id
    assert state.turn_index == 1
    assert state.turn_id != first_turn
    assert state.last_transition is LoopTransition.CONTINUE_AFTER_MODEL
    assert state.status is LoopStatus.RUNNING


def test_agent_runtime_emits_context_compiled_event_before_model_call(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    async def collect() -> list[AgentEvent]:
        return [event async for event in stream_agent_task(agent, "hello")]

    events = asyncio.run(collect())

    compiled_events = [
        event for event in events if isinstance(event, ContextCompiledEvent)
    ]
    model_starts = [event for event in events if isinstance(event, ModelCallStartEvent)]
    assert len(compiled_events) == 1
    assert len(model_starts) == 1
    compiled = compiled_events[0]
    assert compiled.resolved_call == model_starts[0].resolved_call
    assert compiled.budget.resolved_model_call_id == (
        compiled.resolved_call.resolved_model_call_id
    )
    assert compiled.budget.target_fingerprint == (
        compiled.resolved_call.target.target_fingerprint
    )
    assert compiled.context_id == transport.contexts[0].context_id
    assert compiled.model_call_index == transport.contexts[0].model_call_index == 1
    assert compiled.budget.tools_estimated_tokens is not None
    assert compiled.budget.tools_estimated_tokens > 0
    assert any(section["channel"] == "current_user" for section in compiled.sections)


def test_main_model_start_batch_atomically_commits_reply_reservation_and_model_start(
    tmp_path,
) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    asyncio.run(run_agent_task(agent, "hello"))
    events = tuple(agent.runtime_session.event_log.iter())
    reply_start = next(
        i for i, event in enumerate(events) if isinstance(event, ReplyStartEvent)
    )
    reservation = next(
        i
        for i, event in enumerate(events)
        if isinstance(event, RolloutBudgetReservationCreatedEvent)
        and event.reservation.owner_kind == "model_call"
    )
    model_start = next(
        i for i, event in enumerate(events) if isinstance(event, ModelCallStartEvent)
    )
    model_end = next(
        i for i, event in enumerate(events) if isinstance(event, ModelCallEndEvent)
    )
    settlement = next(
        i
        for i, event in enumerate(events)
        if isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.usage_status in {"provider_reported_usage", "reserved_missing_usage"}
    )
    reply_end = next(
        i for i, event in enumerate(events) if isinstance(event, ReplyEndEvent)
    )

    assert (reply_start, reservation, model_start) == tuple(
        range(reply_start, reply_start + 3)
    )
    assert (model_end, settlement, reply_end) == tuple(range(model_end, model_end + 3))
    account_close = next(
        event for event in events if isinstance(event, RolloutBudgetAccountClosedEvent)
    )
    assert account_close.model_call_count == 1
    assert account_close.charged_milliunits > 0


def test_agent_runtime_builds_immutable_context_input_before_compile(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    captured = []
    original = agent_module.prepare_live_context_snapshot

    async def capture_snapshot(**kwargs):
        prepared = await original(**kwargs)
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(agent_module, "prepare_live_context_snapshot", capture_snapshot)
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "snapshot me"))

    assert result.status is LoopStatus.FINISHED
    assert len(captured) == 1
    prepared = captured[0]
    fact = prepared.invocation.fact
    assert fact.current_user_message.text == "snapshot me"
    assert fact.current_user_message.message_id == (
        f"user-message:{result.state.run_id}"
    )
    assert fact.run_entry.run_start.sequence == (
        fact.authority_slice_plan.transcript_window.protected_run_start_sequence
    )
    assert (
        fact.identity.source_through_sequence
        == prepared.authority_slice.through_sequence
    )
    candidate_ids = tuple(
        entry.candidate.source_instance_id
        for entry in prepared.prepared_candidates.entries
    )
    assert "system:prompt" in candidate_ids
    assert "runtime_context" in candidate_ids
    runtime_authority = next(
        authority
        for authority in fact.candidate_authorities
        if authority.source_instance_id == "runtime_context"
    )
    assert runtime_authority.lifecycle_dependency_fingerprint == (
        fact.runtime_environment.fact_fingerprint
    )
    assert (
        f"Workspace root: {fact.runtime_environment.model_visible_workspace_root}"
        in runtime_authority.model_visible_text
    )
    assert (
        f"Current date: {fact.timing.compiled_local_date}"
        in runtime_authority.model_visible_text
    )
    assert prepared.prepared_tool_results.units == (
        prepared.normalized_transcript.tool_result_units
    )
    compiled = next(
        event
        for event in agent.runtime_session.event_log.iter(run_id=result.state.run_id)
        if isinstance(event, ContextCompiledEvent) and event.status == "compiled"
    )
    assert compiled.sequence is not None
    assert fact.identity.source_through_sequence < compiled.sequence
    assert fact.resolved_model_call == compiled.resolved_call
    sections_by_id = {section["id"]: section for section in compiled.sections}
    assert all("timing" in section["metadata"] for section in compiled.sections)
    assert (
        sections_by_id["runtime_context"]["metadata"]["source_timing"]["freshness"]
        == "current_turn"
    )
    assert (
        sections_by_id["transcript:current_user"]["metadata"]["timing"]["source"][
            "freshness"
        ]
        == "current_turn"
    )
    provider_text = "\n".join(
        (
            transport.contexts[0].system_prompt or "",
            *(
                text
                for message in transport.contexts[0].messages
                for text in message.content
            ),
        )
    )
    assert "[context timing: freshness=current_turn;" in provider_text

    from pulsara_agent.runtime.context_input.manifest import (
        ContextInputManifestWriteResult,
        build_context_compile_input_audit,
        build_context_input_manifest_candidate,
    )
    from pulsara_agent.runtime.context_input.replay import (
        load_context_input_manifest,
    )

    assert compiled.input_audit is not None
    manifest = load_context_input_manifest(
        audit=compiled.input_audit,
        archive=agent.runtime_session.archive,
    )
    candidate = build_context_input_manifest_candidate(manifest)
    duplicate = build_context_input_manifest_candidate(manifest)
    assert candidate == duplicate
    assert candidate.canonical_bytes == duplicate.canonical_bytes
    audit = build_context_compile_input_audit(
        manifest=manifest,
        candidate=candidate,
        write_result=ContextInputManifestWriteResult(
            outcome="stored",
            artifact_id=candidate.artifact_id,
            content_fingerprint=candidate.content_fingerprint,
        ),
        transcript_message_count=len(
            prepared.normalized_transcript.transcript.messages
        ),
        transcript_pair_count=len(prepared.normalized_transcript.transcript.tool_pairs),
        tool_result_unit_count=len(prepared.prepared_tool_results.units),
    )
    assert audit.input_aggregate_fingerprint == manifest.input_aggregate_fingerprint
    assert audit.input_manifest_artifact_id == candidate.artifact_id


def test_snapshot_build_failure_emits_typed_pre_manifest_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    def fail_snapshot(_build_input, **_kwargs):
        raise ValueError("synthetic snapshot join failure")

    monkeypatch.setattr(agent_module, "build_context_snapshot", fail_snapshot)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "unused"}])),
    )

    result = asyncio.run(run_agent_task(agent, "trigger snapshot failure"))

    assert result.status is LoopStatus.FAILED
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    failed = [
        event
        for event in events
        if isinstance(event, ContextCompiledEvent) and event.status == "failed"
    ]
    assert len(failed) == 1
    event = failed[0]
    assert event.failure_stage == "snapshot_build"
    assert event.input_audit is None
    assert event.input_failure is not None
    assert event.input_failure.failure_stage == "snapshot_build"
    assert event.input_failure.manifest_write_outcome == "not_attempted"
    assert event.input_failure.snapshot_id is not None
    assert event.input_failure.source_through_sequence is not None
    assert not any(isinstance(item, ModelCallStartEvent) for item in events)


def test_long_horizon_fold_failure_emits_typed_pre_manifest_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    def fail_prepared_facts(**_kwargs):
        raise ValueError("synthetic long-horizon prepared-facts failure")

    monkeypatch.setattr(
        agent_module,
        "_resolve_prepared_long_horizon_context_facts",
        fail_prepared_facts,
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "unused"}])),
    )

    result = asyncio.run(run_agent_task(agent, "trigger long-horizon fold failure"))

    assert result.status is LoopStatus.FAILED
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    failed = tuple(
        event
        for event in events
        if isinstance(event, ContextCompiledEvent) and event.status == "failed"
    )
    assert len(failed) == 1
    assert failed[0].failure_stage == "long_horizon_fold"
    assert failed[0].input_failure is not None
    assert failed[0].input_failure.failure_stage == "long_horizon_fold"
    assert failed[0].input_failure.manifest_write_outcome == "not_attempted"
    assert not any(isinstance(item, ModelCallStartEvent) for item in events)


def test_compiled_event_budget_matches_call_fact(tmp_path) -> None:
    test_agent_runtime_emits_context_compiled_event_before_model_call(tmp_path)


def test_run_start_records_model_target(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )
    asyncio.run(run_agent_task(agent, "record target"))
    started = next(
        event
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, RunStartEvent)
    )
    assert transport.calls
    assert started.model_target == transport.calls[0].target.fact


def _pending_mcp_installation_audit() -> McpPendingInstallationAudit:
    timing = McpServerLifecycleTimingFact(
        queued_at_utc="2026-01-01T00:00:00Z",
        connect_started_at_utc="2026-01-01T00:00:00Z",
        connect_ended_at_utc="2026-01-01T00:00:00.003000Z",
        discovery_started_at_utc="2026-01-01T00:00:00.003000Z",
        discovery_ended_at_utc="2026-01-01T00:00:00.010000Z",
        completed_at_utc="2026-01-01T00:00:00.010000Z",
        connect_duration_seconds=0.003,
        discovery_duration_seconds=0.007,
        total_duration_seconds=0.01,
    )
    attempt = McpReconcileAttemptSummaryFact(
        server_id="docs",
        reconcile_attempt_id="mcp_attempt:atomic",
        reconcile_trigger="initial",
        attempt_status="ready",
        request_count=1,
        page_count=1,
        cache_outcome="miss",
    )
    snapshot = McpInstalledServerSnapshotFact(
        server_id="docs",
        status="ready",
        required=False,
        changed_in_this_installation=True,
        attempt=attempt,
        snapshot_id="mcp_snapshot:atomic",
        discovery_generation=1,
        event_safe_config_fingerprint="sha256:server",
        snapshot_semantic_fingerprint="sha256:catalog",
        tool_count=1,
        lifecycle_timing=timing,
        catalog_artifact_id=None,
    )
    return McpPendingInstallationAudit(
        event_id="mcp_installation_event:atomic",
        installation_id="mcp_installation:atomic",
        previous_installation_id=None,
        config_epoch=1,
        event_safe_config_set_fingerprint="sha256:set",
        installation_triggers=("initial",),
        coalesced_installation_count=0,
        coalesced_attempt_summaries=(),
        coalesced_attempt_summaries_omitted=0,
        server_snapshots=(snapshot,),
        total_installed_tool_count=1,
        added_tool_count=1,
        revoked_tool_count=0,
        changed_tool_names_bounded=("mcp__docs__lookup",),
        changed_tool_names_omitted=0,
        diagnostics=(),
        baseline_tool_names=frozenset(),
        current_tool_names=frozenset({"mcp__docs__lookup"}),
    )


def test_run_start_and_first_mcp_installation_audit_are_one_atomic_batch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    runtime_session = in_memory_runtime_session(tmp_path)
    runtime_session.set_mcp_installation_contract(
        installation_id="mcp_installation:atomic",
        pending_audit=_pending_mcp_installation_audit(),
    )
    recorded_batches: list[tuple[EventType, ...]] = []
    original_extend = InMemoryEventLog.extend_with_materialization_state

    def record_extend(
        self,
        events,
        *,
        expected_account_state_fingerprint,
        resulting_account_state,
        physical_charge_contract,
        expected_last_sequence=None,
        deadline_monotonic=None,
    ):
        batch = tuple(events)
        recorded_batches.append(tuple(event.type for event in batch))
        return original_extend(
            self,
            batch,
            expected_account_state_fingerprint=expected_account_state_fingerprint,
            resulting_account_state=resulting_account_state,
            physical_charge_contract=physical_charge_contract,
            expected_last_sequence=expected_last_sequence,
            deadline_monotonic=deadline_monotonic,
        )

    monkeypatch.setattr(
        InMemoryEventLog,
        "extend_with_materialization_state",
        record_extend,
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    asyncio.run(run_agent_task(agent, "atomic installation"))

    assert recorded_batches[0][:4] == (
        EventType.RUN_START,
        EventType.CONTEXT_WINDOW_OPENED,
        EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED,
        EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED,
    )
    assert recorded_batches[0][-1] is EventType.LEDGER_MATERIALIZATION_ACCOUNT_GENESIS
    stored = runtime_session.event_log.iter()
    assert isinstance(stored[0], RunStartEvent)
    assert isinstance(stored[3], McpCapabilitySnapshotInstalledEvent)
    assert stored[0].mcp_installation_id == stored[3].installation_id
    assert not runtime_session._pending_mcp_installation_audits


def test_failed_run_start_audit_batch_keeps_audit_pending_and_writes_nothing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = ScriptedTransport([{"text": "must not run"}])
    runtime_session = in_memory_runtime_session(tmp_path)
    runtime_session.set_mcp_installation_contract(
        installation_id="mcp_installation:atomic",
        pending_audit=_pending_mcp_installation_audit(),
    )

    def fail_before_commit(
        self,
        events,
        *,
        expected_account_state_fingerprint,
        resulting_account_state,
        physical_charge_contract,
        expected_last_sequence=None,
        deadline_monotonic=None,
    ):
        raise RuntimeError("synthetic pre-commit failure")

    monkeypatch.setattr(
        InMemoryEventLog,
        "extend_with_materialization_state",
        fail_before_commit,
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    with pytest.raises(Exception, match="Event batch commit failed"):
        asyncio.run(run_agent_task(agent, "atomic failure"))

    assert runtime_session.event_log.iter() == []
    assert len(runtime_session._pending_mcp_installation_audits) == 1
    assert transport.contexts == []


def test_pending_mcp_installation_audits_coalesce_to_last_durable_parent(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    first = _pending_mcp_installation_audit()
    first_snapshot = first.server_snapshots[0]
    second_attempt = first_snapshot.attempt.model_copy(
        update={
            "reconcile_attempt_id": "mcp_attempt:ttl",
            "reconcile_trigger": "ttl_refresh",
        }
    )
    second_snapshot = first_snapshot.model_copy(update={"attempt": second_attempt})
    second = replace(
        first,
        event_id="mcp_installation_event:ttl",
        installation_id="mcp_installation:ttl",
        previous_installation_id=first.installation_id,
        installation_triggers=("ttl_refresh",),
        server_snapshots=(second_snapshot,),
        added_tool_count=0,
        baseline_tool_names=first.current_tool_names,
        current_tool_names=first.current_tool_names,
    )

    runtime_session.set_mcp_installation_contract(
        installation_id=first.installation_id,
        pending_audit=first,
    )
    runtime_session.set_mcp_installation_contract(
        installation_id=second.installation_id,
        pending_audit=second,
    )

    pending = runtime_session._pending_mcp_installation_audits
    assert len(pending) == 1
    assert pending[0].installation_id == "mcp_installation:ttl"
    assert pending[0].previous_installation_id is None
    assert pending[0].coalesced_installation_count == 1
    assert pending[0].installation_triggers == ("initial", "ttl_refresh")
    assert pending[0].coalesced_attempt_summaries == (first_snapshot.attempt,)


def test_run_start_post_commit_publication_failure_acknowledges_mcp_audit(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    runtime_session.set_mcp_installation_contract(
        installation_id="mcp_installation:atomic",
        pending_audit=_pending_mcp_installation_audit(),
    )

    class FailInstallationAudit:
        async def on_published_event(self, published: RuntimePublishedEvent) -> None:
            if isinstance(
                published.event,
                McpCapabilitySnapshotInstalledEvent,
            ):
                raise RuntimeError("synthetic audit observer failure")

    failing = FailInstallationAudit()
    runtime_session.publisher.subscribe(failing)
    transport = ScriptedTransport([{"text": "second run"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    with pytest.raises(EventPublicationAfterCommitError):
        asyncio.run(run_agent_task(agent, "committed first run"))

    assert runtime_session._pending_mcp_installation_audits == []
    assert (
        len(
            [
                event
                for event in runtime_session.event_log.iter()
                if isinstance(event, McpCapabilitySnapshotInstalledEvent)
            ]
        )
        == 1
    )

    runtime_session.publisher.unsubscribe(failing)
    result = asyncio.run(run_agent_task(agent, "second run has no duplicate audit"))
    assert result.final_text == "second run"


def test_mcp_terminal_post_commit_failure_folds_state_and_releases_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    class LeaseSupervisor:
        def __init__(self) -> None:
            self.completed: list[str] = []
            self.close_calls = 0

        def complete_pending_lease(self, interaction_id: str) -> None:
            self.completed.append(interaction_id)

        async def close_retiring_slots(self, **_kwargs) -> None:
            self.close_calls += 1

    supervisor = LeaseSupervisor()
    runtime_session.mcp_supervisor = supervisor

    class FailTerminalResult:
        async def on_published_event(self, published: RuntimePublishedEvent) -> None:
            if isinstance(published.event, ToolResultEndEvent):
                raise RuntimeError("synthetic terminal observer failure")

    runtime_session.publisher.subscribe(FailTerminalResult())
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([])),
    )
    empty_exposure = CapabilityExposurePlan(
        registry_generation=0,
        direct_tool_specs=(),
        direct_names=frozenset(),
        deferred_names=frozenset(),
        hidden_names=frozenset(),
        callable_names=frozenset(),
        descriptors_by_name={},
        catalog_entries=(),
        active_injections=(),
        catalog_prompt=None,
        active_skill_prompt=None,
        diagnostics=(),
    )
    monkeypatch.setattr(
        agent,
        "_require_capability_exposure",
        lambda _state: empty_exposure,
    )
    state = agent.new_state()
    state.status = LoopStatus.WAITING_USER
    state.stop_reason = "waiting_user"
    state.pending_interaction_kind = "mcp_input_required"
    state.pending_interaction_payload = {
        "interaction_id": "mcp_input_required:post-commit",
        "tool_call_id": "call:mcp-post-commit",
        "tool_name": "mcp__docs__lookup",
    }

    async def terminal_result(self, current_state, _resolution):
        current_state.pending_interaction_kind = None
        current_state.pending_interaction_payload = {}
        current_state.status = LoopStatus.RUNNING
        current_state.stop_reason = None
        async for event in self._emit_tool_result_and_record(
            current_state,
            tool_call_id="call:mcp-post-commit",
            tool_call_name="mcp__docs__lookup",
            output="terminal result committed",
            result_state=ToolResultState.ERROR,
        ):
            yield event

    monkeypatch.setattr(
        AgentRuntime,
        "_stream_mcp_input_required_resolution",
        terminal_result,
    )
    resolution = McpInputRequiredInteractionResolution(
        interaction_id="mcp_input_required:post-commit",
        responses={},
    )

    async def run() -> None:
        with pytest.raises(EventPublicationAfterCommitError):
            async for _ in agent.stream_after_mcp_input_required(state, resolution):
                pass

    asyncio.run(run())

    assert [result.id for result in state.tool_results] == ["call:mcp-post-commit"]
    assert state.pending_interaction_kind is None
    assert supervisor.completed == ["mcp_input_required:post-commit"]
    assert supervisor.close_calls == 1


def test_cancel_during_mcp_suspension_publication_wait_confirms_and_preserves_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    class LeaseSupervisor:
        def __init__(self) -> None:
            self.confirmed: list[tuple[str, str]] = []
            self.aborted: list[tuple[str, str]] = []

        def confirm_pending_lease(
            self, interaction_id: str, reservation_id: str
        ) -> None:
            self.confirmed.append((interaction_id, reservation_id))

        def abort_pending_lease(self, interaction_id: str, reservation_id: str) -> None:
            self.aborted.append((interaction_id, reservation_id))

    supervisor = LeaseSupervisor()
    runtime_session.mcp_supervisor = supervisor
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([])),
    )
    state = agent.new_state()
    committed = asyncio.Event()
    target = agent.resolve_run_model_target().fact
    prepared_run = prepare_root_long_horizon_run(
        runtime_session_id=runtime_session.runtime_session_id,
        run_id=state.run_id,
        run_start_event_id=f"run_start:test:{state.run_id}",
        primary_target=target,
        summarizer_target=target,
        graph_reducer_contract=(
            runtime_session.subagent_graph_checkpoint_service.reducer_binding.contract
        ),
        source_through_sequence_at_open=0,
        initial_projection_unit_count=0,
        initial_projection_state_fingerprint=empty_projection_state_fingerprint(),
    )
    account = prepared_run.root_account
    assert account is not None
    reservation_payload = {
        "reservation_id": "rollout_reservation:tool:cancel-suspend",
        "account_id": account.account_id,
        "owner_kind": "tool_call",
        "owner_id": "call:mcp-suspend-cancel",
        "phase_at_reservation": RolloutPhase.EXPLORATION,
        "budget_bucket": RolloutBudgetBucket.EXPLORATION,
        "reserved_milliunits": account.policy.tool_cost_unit_weight_milli,
        "model_call_reservation_quote": None,
        "source_sequence": 1,
    }
    reservation = RolloutReservationFact(
        **reservation_payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-reservation:v1", reservation_payload
        ),
    )
    suspended = ToolExecutionSuspended(
        tool_call_id="call:mcp-suspend-cancel",
        tool_name="mcp__docs__lookup",
        interaction_kind="mcp_input_required",
        payload={
            "interaction_id": "mcp_input_required:suspend-cancel",
            "mcp_pending_lease_reservation_id": "reservation:suspend-cancel",
        },
    )

    async def run() -> None:
        await runtime_session.write_events(
            (
                RolloutBudgetAccountOpenedEvent(
                    id=f"rollout_budget_account_opened:{account.account_id}",
                    run_id=state.run_id,
                    turn_id=state.turn_id,
                    reply_id=state.reply_id,
                    account=account,
                ),
                RolloutBudgetReservationCreatedEvent(
                    id="rollout_budget_reservation_created:tool:cancel-suspend",
                    run_id=state.run_id,
                    turn_id=state.turn_id,
                    reply_id=state.reply_id,
                    reservation=reservation,
                ),
            ),
            expected_last_sequence=0,
            state=state,
        )
        runtime_session.tool_execution_terminal_registry.install_admitted_batch(
            run_id=state.run_id,
            reservations=(reservation,),
        )
        original_commit_suspension = (
            RuntimeSessionToolExecutionEventCommitPort.commit_suspension
        )

        async def commit_then_wait(
            self,
            *,
            suspension_candidate,
            reservation_id,
            expected_reservation_fingerprint,
        ):
            assert self.runtime_session is runtime_session
            result = await original_commit_suspension(
                self,
                suspension_candidate=suspension_candidate,
                reservation_id=reservation_id,
                expected_reservation_fingerprint=(
                    expected_reservation_fingerprint
                ),
            )
            committed.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as cancelled:
                raise EventWriteCancelled(
                    EventBatchCommitOutcome(
                        status="full",
                        deadline_monotonic=time.monotonic(),
                        result=result,
                    )
                ) from cancelled
            return result

        monkeypatch.setattr(
            RuntimeSessionToolExecutionEventCommitPort,
            "commit_suspension",
            commit_then_wait,
        )

        async def consume() -> None:
            async for _ in agent._suspend_tool_execution(
                state,
                suspended,
                reservation=reservation,
            ):
                pass

        task = asyncio.create_task(consume())
        await committed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    suspension_events = [
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ToolExecutionSuspendedEvent)
    ]
    assert len(suspension_events) == 1
    assert supervisor.confirmed == [
        (
            "mcp_input_required:suspend-cancel",
            "reservation:suspend-cancel",
        )
    ]
    assert supervisor.aborted == []
    owner = runtime_session.tool_execution_terminal_registry.owner_for_call(
        run_id=state.run_id,
        tool_call_id="call:mcp-suspend-cancel",
    )
    assert owner is not None and owner.state == "suspended"
    assert state.status is LoopStatus.WAITING_USER
    assert state.pending_interaction_kind == "mcp_input_required"


def test_suspension_precommit_none_terminalizes_reservation_without_orphan(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass(slots=True)
    class SuspendingTool:
        name: str = "suspending_tool"
        description: str = "Return one MCP-style suspended interaction."
        parameters: dict = field(
            default_factory=lambda: {"type": "object", "properties": {}}
        )
        is_read_only: bool = True
        is_concurrency_safe: bool = True

        async def execute_async(
            self,
            call: ToolCall,
            *,
            runtime_context: ToolRuntimeContext,
        ) -> ToolExecutionSuspended:
            del runtime_context
            return ToolExecutionSuspended(
                tool_call_id=call.id,
                tool_name=call.name,
                interaction_kind="mcp_input_required",
                payload={
                    "interaction_id": "mcp_input_required:precommit-none",
                    "mcp_pending_lease_reservation_id": "lease:precommit-none",
                    "original_request": {
                        "arguments": {},
                    },
                },
            )

    class LeaseSupervisor:
        def __init__(self) -> None:
            self.aborted: list[tuple[str, str]] = []

        def abort_pending_lease(self, interaction_id: str, reservation_id: str) -> None:
            self.aborted.append((interaction_id, reservation_id))

    runtime_session = in_memory_runtime_session(tmp_path)
    supervisor = LeaseSupervisor()
    runtime_session.mcp_supervisor = supervisor
    registry = ToolRegistry()
    registry.register(SuspendingTool())
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(
            ScriptedTransport(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "call:suspension-precommit-none",
                                "name": "suspending_tool",
                                "arguments": "{}",
                            }
                        ]
                    }
                ]
            )
        ),
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)

    async def fail_before_commit(self, **_kwargs):
        assert self.runtime_session is runtime_session
        raise EventCommitError("synthetic suspension precommit failure")

    monkeypatch.setattr(
        RuntimeSessionToolExecutionEventCommitPort,
        "commit_suspension",
        fail_before_commit,
    )

    with pytest.raises(EventCommitError, match="synthetic suspension precommit failure"):
        asyncio.run(run_agent_task(agent, "suspend once"))

    assert not any(
        isinstance(event, ToolExecutionSuspendedEvent)
        for event in runtime_session.event_log.iter()
    )
    terminal = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:suspension-precommit-none"
    )
    assert terminal.state is ToolResultState.ERROR
    assert any(
        isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.source_tool_result_event_id == terminal.id
        for event in runtime_session.event_log.iter()
    )
    assert supervisor.aborted == [
        ("mcp_input_required:precommit-none", "lease:precommit-none")
    ]
    assert runtime_session.tool_execution_terminal_registry.active_owner_count() == 0


def test_mcp_suspension_retains_exact_physical_tail_until_terminal_result(
    tmp_path,
) -> None:
    @dataclass(slots=True)
    class SuspendingTool:
        name: str = "suspending_tool"
        description: str = "Return one MCP-style suspended interaction."
        parameters: dict = field(
            default_factory=lambda: {"type": "object", "properties": {}}
        )
        is_read_only: bool = True
        is_concurrency_safe: bool = True

        async def execute_async(
            self,
            call: ToolCall,
            *,
            runtime_context: ToolRuntimeContext,
        ) -> ToolExecutionSuspended:
            del runtime_context
            return ToolExecutionSuspended(
                tool_call_id=call.id,
                tool_name=call.name,
                interaction_kind="mcp_input_required",
                payload={
                    "interaction_id": "mcp_input_required:physical-tail",
                    "mcp_pending_lease_reservation_id": "lease:physical-tail",
                    "original_request": {"arguments": {}},
                },
            )

    class LeaseSupervisor:
        def __init__(self) -> None:
            self.confirmed: list[tuple[str, str]] = []

        def confirm_pending_lease(
            self,
            interaction_id: str,
            reservation_id: str,
        ) -> None:
            self.confirmed.append((interaction_id, reservation_id))

    runtime_session = in_memory_runtime_session(tmp_path)
    supervisor = LeaseSupervisor()
    runtime_session.mcp_supervisor = supervisor
    registry = ToolRegistry()
    registry.register(SuspendingTool())
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(
            ScriptedTransport(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "call:mcp-physical-tail",
                                "name": "suspending_tool",
                                "arguments": "{}",
                            }
                        ]
                    }
                ]
            )
        ),
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)
    state = agent.new_state()

    first = asyncio.run(run_agent_task(agent, "suspend once", state=state))

    assert first.status is LoopStatus.WAITING_USER
    reservation_event = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, PhysicalOperationReservationCreatedEvent)
        and event.reservation.owner_id == "call:mcp-physical-tail"
    )
    suspension_event = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, PhysicalOperationReservationSuspendedEvent)
        and event.suspension.owner_id == "call:mcp-physical-tail"
    )
    active = runtime_session.materialization_account_store.snapshot()
    assert active is not None and len(active.active_reservations) == 1
    suspended_state = active.active_reservations[0]
    assert suspended_state.lifecycle_status == "suspended_tail"
    assert suspended_state.reservation_id == reservation_event.reservation.reservation_id
    assert suspended_state.latest_lifecycle_event_id == suspension_event.id
    assert suspended_state.remaining_events == (
        reservation_event.reservation.terminal_tail_reserved_events
    )
    assert suspended_state.remaining_payload_bytes == (
        reservation_event.reservation.terminal_tail_reserved_payload_bytes
    )
    assert supervisor.confirmed == [
        ("mcp_input_required:physical-tail", "lease:physical-tail")
    ]

    rollout_reservation = agent._pending_tool_rollout_reservation(
        state.pending_interaction_payload,
        run_id=state.run_id,
    )

    async def terminalize() -> None:
        async for _ in agent._emit_tool_result_and_record(
            state,
            tool_call_id="call:mcp-physical-tail",
            tool_call_name="suspending_tool",
            output="input-required interaction cancelled",
            result_state=ToolResultState.ERROR,
            tool_observation_timing_seed=dict(
                state.pending_interaction_payload.get(
                    "tool_observation_timing_seed", {}
                )
            )
            or None,
            rollout_reservation=rollout_reservation,
        ):
            pass

    asyncio.run(terminalize())

    settlement = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, PhysicalOperationReservationSettledEvent)
        and event.settlement.reservation_id
        == reservation_event.reservation.reservation_id
    )
    assert settlement.settlement.predecessor_status == "suspended_tail"
    assert settlement.settlement.terminal_outcome == "runtime_error"
    account = runtime_session.materialization_account_store.snapshot()
    assert account is not None and account.active_reservations == ()
    assert runtime_session.tool_execution_terminal_registry.active_owner_count() == 0


@pytest.mark.parametrize(
    ("failure_mode", "expect_terminal", "expect_latched"),
    [
        ("precommit", False, False),
        ("cancel_after_commit", True, False),
        ("confirmation_unknown", False, True),
    ],
)
def test_mcp_resume_terminal_precommit_full_unknown_and_cancel_after_commit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expect_terminal: bool,
    expect_latched: bool,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    class LeaseSupervisor:
        def __init__(self) -> None:
            self.completed: list[str] = []

        def complete_pending_lease(self, interaction_id: str) -> None:
            self.completed.append(interaction_id)

        async def close_retiring_slots(self, **_kwargs) -> None:
            return None

    supervisor = LeaseSupervisor()
    runtime_session.mcp_supervisor = supervisor
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([])),
    )
    empty_exposure = CapabilityExposurePlan(
        registry_generation=0,
        direct_tool_specs=(),
        direct_names=frozenset(),
        deferred_names=frozenset(),
        hidden_names=frozenset(),
        callable_names=frozenset(),
        descriptors_by_name={},
        catalog_entries=(),
        active_injections=(),
        catalog_prompt=None,
        active_skill_prompt=None,
        diagnostics=(),
    )
    monkeypatch.setattr(
        agent,
        "_require_capability_exposure",
        lambda _state: empty_exposure,
    )
    interaction_id = f"mcp_input_required:{failure_mode}"
    tool_call_id = f"call:mcp-{failure_mode}"
    state = agent.new_state()
    state.status = LoopStatus.WAITING_USER
    state.stop_reason = "waiting_user"
    state.pending_interaction_kind = "mcp_input_required"
    state.pending_interaction_payload = {
        "interaction_id": interaction_id,
        "tool_call_id": tool_call_id,
        "tool_name": "mcp__docs__lookup",
    }

    async def terminal_result(self, current_state, _resolution):
        current_state.pending_interaction_kind = None
        current_state.pending_interaction_payload = {}
        current_state.status = LoopStatus.RUNNING
        current_state.stop_reason = None
        async for event in self._emit_tool_result_and_record(
            current_state,
            tool_call_id=tool_call_id,
            tool_call_name="mcp__docs__lookup",
            output="terminal result",
            result_state=ToolResultState.ERROR,
        ):
            yield event

    monkeypatch.setattr(
        AgentRuntime,
        "_stream_mcp_input_required_resolution",
        terminal_result,
    )
    committed = asyncio.Event()

    if failure_mode == "precommit":

        async def fail_before_commit(self, _events, *, state=None):
            assert self is runtime_session
            del state
            raise EventCommitError("synthetic precommit failure")

        monkeypatch.setattr(type(runtime_session), "emit_many", fail_before_commit)
    elif failure_mode == "cancel_after_commit":

        async def commit_then_wait(self, events, *, state=None):
            assert self is runtime_session
            result = await runtime_session.write_events(tuple(events), state=state)
            committed.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as cancelled:
                raise EventWriteCancelled(
                    EventBatchCommitOutcome(
                        status="full",
                        deadline_monotonic=time.monotonic(),
                        result=result,
                    )
                ) from cancelled
            return list(result.committed_events)

        monkeypatch.setattr(type(runtime_session), "emit_many", commit_then_wait)
    else:

        async def fail_before_unknown_confirmation(self, _events, *, state=None):
            assert self is runtime_session
            del state
            raise EventCommitError(
                "synthetic unknown commit acknowledgement",
                commit_outcome="unknown",
            )

        monkeypatch.setattr(
            type(runtime_session),
            "emit_many",
            fail_before_unknown_confirmation,
        )

    resolution = McpInputRequiredInteractionResolution(
        interaction_id=interaction_id,
        responses={},
    )

    async def run() -> None:
        async def consume() -> None:
            async for _ in agent.stream_after_mcp_input_required(state, resolution):
                pass

        task = asyncio.create_task(consume())
        if failure_mode == "cancel_after_commit":
            await committed.wait()
            task.cancel()
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await task

    asyncio.run(run())

    terminal_events = [
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ToolResultEndEvent) and event.tool_call_id == tool_call_id
    ]
    assert bool(terminal_events) is expect_terminal
    assert runtime_session.reconciliation_required is expect_latched
    if expect_terminal:
        assert state.pending_interaction_kind is None
        assert [result.id for result in state.tool_results] == [tool_call_id]
        assert supervisor.completed == [interaction_id]
    else:
        assert state.status is LoopStatus.WAITING_USER
        assert state.pending_interaction_kind == "mcp_input_required"
        assert state.pending_interaction_payload["interaction_id"] == interaction_id
        assert state.tool_results == []
        assert supervisor.completed == []


def test_model_call_rejected_event_is_inspectable(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "must not run"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    def reject_start_stream(**kwargs):
        del kwargs
        raise ModelContextIdentityMismatch("synthetic compiled identity rejection")

    agent.llm_runtime.start_stream = reject_start_stream
    result = asyncio.run(run_agent_task(agent, "reject before provider"))
    rejected = [
        event
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, ModelCallRejectedEvent)
    ]
    assert result.status is LoopStatus.FAILED
    assert len(rejected) == 1
    assert rejected[0].reason_code == "model_context_identity_mismatch"
    assert transport.contexts == []


def test_pr1_compiled_validation_rejection_uses_existing_durable_failure_path(
    tmp_path,
) -> None:
    test_model_call_rejected_event_is_inspectable(tmp_path)


def test_agent_runtime_fails_cleanly_when_current_user_exceeds_context_budget(
    tmp_path,
) -> None:
    transport = ScriptedTransport([{"text": "should not be called"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "x" * 900_000))

    assert result.status is LoopStatus.FAILED
    assert result.error_message is not None
    assert "Current user input exceeds" in result.error_message
    assert transport.contexts == []
    compiled_events = [
        event
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, ContextCompiledEvent)
    ]
    assert [event.status for event in compiled_events] == ["pressure", "failed"]
    assert (
        compiled_events[0].model_call_index == compiled_events[1].model_call_index == 1
    )
    assert (
        compiled_events[0].compile_attempt_index
        == compiled_events[1].compile_attempt_index
        == 1
    )
    assert (
        compiled_events[0].context_retry_index
        == compiled_events[1].context_retry_index
        == 0
    )
    assert compiled_events[0].resolved_call == compiled_events[1].resolved_call
    assert all(
        event.budget.target_fingerprint == event.resolved_call.target.target_fingerprint
        for event in compiled_events
    )


def test_context_pressure_event_records_resolved_call(tmp_path) -> None:
    test_agent_runtime_fails_cleanly_when_current_user_exceeds_context_budget(tmp_path)


def test_context_failed_event_records_real_budget(tmp_path) -> None:
    test_agent_runtime_fails_cleanly_when_current_user_exceeds_context_budget(tmp_path)


def test_run_followups_reuse_target(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:unknown",
                        "name": "unknown_contract_tool",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": "finished after tool result"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "exercise a tool follow-up"))

    assert result.status is LoopStatus.FINISHED
    starts = [
        event
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, ModelCallStartEvent)
    ]
    assert len(starts) == 2
    assert starts[0].resolved_call.target == starts[1].resolved_call.target
    assert (
        starts[0].resolved_call.resolved_model_call_id
        != starts[1].resolved_call.resolved_model_call_id
    )
    events = agent.runtime_session.event_log.iter()
    rewrites = tuple(
        event
        for event in events
        if isinstance(event, ContextProjectionRewritePageEvent)
    )
    assert len(rewrites) == 1
    result_end = next(
        event
        for event in events
        if isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:unknown"
    )
    assert result_end.sequence is not None
    assert starts[1].sequence is not None
    assert result_end.sequence < rewrites[0].sequence < starts[1].sequence
    run_id = next(event.run_id for event in events if isinstance(event, RunStartEvent))
    projection = agent.runtime_session.long_horizon_state_store.active_projection_state(
        run_id
    )
    assert projection is None
    closed_window = next(
        event for event in events if isinstance(event, ContextWindowClosedEvent)
    )
    assert closed_window.final_projection_generation == 1


def test_tool_followup_uses_new_call_id(tmp_path) -> None:
    test_run_followups_reuse_target(tmp_path)


def test_agent_runtime_finishes_text_only_reply(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Say done"))

    assert result.status is LoopStatus.FINISHED
    assert result.stop_reason == "final"
    assert result.final_text == "done"
    assert any(
        event.type is EventType.TEXT_BLOCK_SEGMENT
        for event in agent.runtime_session.event_log.iter()
    )
    assert (
        agent.runtime_session.event_log.replay(result.state.reply_id).content[0].text
        == "done"
    )


def test_agent_runtime_injects_runtime_context_prompt(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
        workspace_kind="project",
    )

    result = asyncio.run(run_agent_task(agent, "Say done"))

    assert result.status is LoopStatus.FINISHED
    system_prompt = transport.contexts[0].system_prompt or ""
    context_text = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    assert "<runtime-context>" not in system_prompt
    assert "<runtime-context>" in context_text
    assert f"Workspace root: {tmp_path.resolve()}" in context_text
    assert "Workspace kind: project" in context_text
    assert f"Terminal current cwd: {tmp_path.resolve()}" in context_text
    assert (
        "Terminal workdir, when provided, must stay inside workspace_root"
        in context_text
    )
    assert (
        "Read-only filesystem tools may read ordinary text files outside workspace_root"
        in context_text
    )
    assert runtime_session.terminal_sessions.session_count() == 0


def test_runtime_emit_from_single_cancelled_task_reaches_subscriber(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    state = LoopState(session_id=runtime_session.runtime_session_id)
    run_start = RunStartEvent(
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        **run_start_permission_fields(
            state.run_id,
            user_input="",
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        ),
        user_input_chars=0,
    )
    delivered: list[AgentEvent] = []

    class Subscriber:
        async def on_published_event(self, published: RuntimePublishedEvent) -> None:
            delivered.append(published.event)

    runtime_session.publisher.subscribe(Subscriber())

    async def run_and_emit_after_cancel() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await runtime_session.emit(
                RunEndEvent(
                    **run_end_contract_fields(
                        state.run_id, status="aborted", abort_kind="user_stop"
                    ),
                    **EventContext(
                        run_id=state.run_id,
                        turn_id=state.turn_id,
                        reply_id=state.reply_id,
                    ).event_fields(),
                    status="aborted",
                    stop_reason="aborted",
                    abort_kind="user_stop",
                ),
                state=state,
            )

    async def run() -> None:
        await runtime_session.emit(run_start, state=state)
        task = asyncio.create_task(run_and_emit_after_cancel())
        await asyncio.sleep(0)
        task.cancel()
        await task

    asyncio.run(run())

    assert any(
        isinstance(event, RunEndEvent) and event.status == "aborted"
        for event in delivered
    )


def test_agent_runtime_does_not_compile_uncommitted_prior_messages(tmp_path) -> None:
    prior = [UserMsg(name="user", content="previous sentinel")]
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "current", prior_messages=prior))

    assert result.status is LoopStatus.FINISHED
    context_text = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    assert "previous sentinel" not in context_text
    assert "current" in context_text


def test_agent_runtime_dispatches_event_and_completed_text_block_hooks(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    seen_events: list[EventType] = []
    seen_blocks: list[str] = []

    runtime_session.hook_manager.register_event(
        None, lambda context, event: seen_events.append(event.type)
    )
    runtime_session.hook_manager.register_block(
        None, lambda context, completion: seen_blocks.append(completion.block_type)
    )
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Say done"))

    assert result.status is LoopStatus.FINISHED
    assert EventType.TEXT_BLOCK_SEGMENT in seen_events
    assert "text" in seen_blocks


def test_agent_runtime_executes_tool_then_finishes(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello from file", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "I read it."},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Read note.txt"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "I read it."
    assert any(
        isinstance(event, ToolResultStartEvent)
        for event in agent.runtime_session.event_log.iter()
    )
    assert len(transport.contexts) == 2
    second_context_text = "\n".join(
        text for msg in transport.contexts[1].messages for text in msg.content
    )
    assert "hello from file" in second_context_text
    compiled = [
        event
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, ContextCompiledEvent)
    ]
    assert compiled[0].tool_result_render_decision_facts == ()
    assert len(compiled[1].tool_result_render_decision_facts) == 1
    assert (
        compiled[1].tool_result_render_decision_facts[0].unit_id
        == compiled[1].tool_result_render_operational_facts[0].unit_id
    )
    assert (
        agent.runtime_session.tool_execution_terminal_registry.active_owner_count()
        == 0
    )


def test_render_cache_write_failure_cannot_block_model_followup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "note.txt").write_text("cache failure survives", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:cache-failure",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "continued after cache failure"},
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    def fail_put(_key, _hint) -> None:
        raise RuntimeError("synthetic cache write failure")

    monkeypatch.setattr(runtime_session.tool_result_render_cache, "put", fail_put)
    result = asyncio.run(run_agent_task(agent, "Read note.txt"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "continued after cache failure"
    assert runtime_session.context_input_cache_diagnostics() == (
        {
            "cache_kind": "tool_result_render",
            "operation": "write",
            "error_type": "RuntimeError",
            "message": "synthetic cache write failure",
        },
    )


def test_agent_runtime_runs_context_compactor_before_tool_followup(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello from file", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "I read it."},
        ]
    )
    compactor = RecordingContextCompactor()
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        context_compactor=compactor,
    )

    result = asyncio.run(run_agent_task(agent, "Read note.txt"))

    assert result.status is LoopStatus.FINISHED
    assert len(compactor.calls) == 1
    transition, pending_count, visible_count = compactor.calls[0]
    assert transition is LoopTransition.CONTINUE_AFTER_TOOL
    assert pending_count == 1
    assert visible_count >= 3


def test_agent_runtime_dispatches_tool_result_hooks(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hook file", encoding="utf-8")
    runtime_session = in_memory_runtime_session(tmp_path)
    seen_tool_result_events: list[EventType] = []
    seen_tool_result_blocks: list[str] = []

    runtime_session.hook_manager.register_event(
        None,
        lambda context, event: (
            seen_tool_result_events.append(event.type)
            if event.type.name.startswith("TOOL_RESULT")
            else None
        ),
    )
    runtime_session.hook_manager.register_block(
        "tool_result",
        lambda context, completion: seen_tool_result_blocks.append(
            completion.block.output[0].text
        ),
    )
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Read note.txt"))

    assert result.status is LoopStatus.FINISHED
    assert seen_tool_result_events == [
        EventType.TOOL_RESULT_START,
        EventType.TOOL_RESULT_TEXT_DELTA,
        EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED,
        EventType.TOOL_RESULT_END,
    ]
    assert any("hook file" in text for text in seen_tool_result_blocks)


def test_agent_runtime_hook_error_does_not_break_run(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    def failing_hook(context, event) -> None:
        if event.type is EventType.TEXT_BLOCK_SEGMENT:
            raise RuntimeError("observer failed")

    runtime_session.hook_manager.register_event(None, failing_hook)
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Say done"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "done"
    assert len(runtime_session.hook_manager.errors) == 1
    assert runtime_session.hook_manager.errors[0].message == "observer failed"


def test_tool_result_lookup_does_not_cross_runs_with_reused_tool_call_id(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    (tmp_path / "note.txt").write_text("OLD", encoding="utf-8")
    first_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "first done"},
        ]
    )
    first_agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(first_transport),
    )

    first_result = asyncio.run(run_agent_task(first_agent, "Read note.txt"))
    assert first_result.status is LoopStatus.FINISHED

    (tmp_path / "note.txt").write_text("NEW", encoding="utf-8")
    second_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "second done"},
        ]
    )
    second_agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(second_transport),
    )

    second_result = asyncio.run(run_agent_task(second_agent, "Read note.txt again"))
    message_output = "\n".join(
        output.text
        for message in second_result.messages
        if message.role == "tool_result"
        for result in message.content
        if isinstance(result, ToolResultBlock)
        for output in result.output
        if isinstance(output, TextBlock)
    )
    second_context_text = "\n".join(
        text for msg in second_transport.contexts[1].messages for text in msg.content
    )

    assert second_result.status is LoopStatus.FINISHED
    assert "NEW" in message_output
    assert "OLD" not in message_output
    assert "NEW" in second_context_text
    assert "OLD" not in second_context_text


def test_malformed_tool_json_emits_standard_tool_result_error(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:bad", "name": "read_file", "arguments": '{"path"'}
                ]
            },
            {"text": "Recovered."},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Use a malformed tool."))
    events = agent.runtime_session.event_log.iter()
    result_events = [
        event for event in events if getattr(event, "tool_call_id", None) == "call:bad"
    ]

    assert result.status is LoopStatus.FINISHED
    assert [
        event.type
        for event in result_events
        if event.type.name.startswith("TOOL_RESULT")
    ] == [
        EventType.TOOL_RESULT_START,
        EventType.TOOL_RESULT_TEXT_DELTA,
        EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED,
        EventType.TOOL_RESULT_END,
    ]
    assert isinstance(result_events[-1], ToolResultEndEvent)
    assert result_events[-1].state is ToolResultState.ERROR
    replayed = agent.runtime_session.event_log.replay(result_events[0].reply_id)
    block = next(
        block for block in replayed.content if isinstance(block, ToolResultBlock)
    )
    assert block.state is ToolResultState.ERROR
    second_context_text = "\n".join(
        text for msg in transport.contexts[1].messages for text in msg.content
    )
    assert "Malformed JSON arguments" in second_context_text


def test_malformed_tool_json_reused_id_does_not_replay_prior_error(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    first_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:bad", "name": "read_file", "arguments": "[]"}
                ]
            },
            {"text": "first recovered"},
        ]
    )
    first_agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(first_transport),
    )
    asyncio.run(run_agent_task(first_agent, "bad first"))

    second_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:bad", "name": "read_file", "arguments": '{"second"'}
                ]
            },
            {"text": "second recovered"},
        ]
    )
    second_agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(second_transport),
    )

    second_result = asyncio.run(run_agent_task(second_agent, "bad second"))
    message_output = "\n".join(
        output.text
        for message in second_result.messages
        if message.role == "tool_result"
        for result in message.content
        if isinstance(result, ToolResultBlock)
        for output in result.output
        if isinstance(output, TextBlock)
    )
    second_context_text = "\n".join(
        text for msg in second_transport.contexts[1].messages for text in msg.content
    )

    assert "Malformed JSON arguments" in message_output
    assert "must be a JSON object" not in message_output
    assert "Malformed JSON arguments" in second_context_text
    assert "must be a JSON object" not in second_context_text


def test_unknown_tool_becomes_error_observation(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:missing", "name": "missing_tool", "arguments": "{}"}
                ]
            },
            {"text": "Recovered from missing tool."},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Call a missing tool."))
    second_context_text = "\n".join(
        text for msg in transport.contexts[1].messages for text in msg.content
    )

    assert result.status is LoopStatus.FINISHED
    assert result.state.in_run_recovery is not None
    assert result.state.in_run_recovery.cause is InRunRecoveryCause.TOOL_FAILURE
    assert result.state.in_run_recovery.consecutive_failures == 1
    assert "Unknown tool: missing_tool" in second_context_text
    assert any(
        isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:missing"
        and event.state is ToolResultState.ERROR
        for event in agent.runtime_session.event_log.iter()
    )


def test_model_failure_sets_typed_in_run_recovery_state(tmp_path) -> None:
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([])),
    )
    state = agent.new_state()

    should_continue = agent._recover_or_fail_model(state)

    assert should_continue is True
    assert state.in_run_recovery is not None
    assert state.in_run_recovery.cause is InRunRecoveryCause.MODEL_FAILURE
    assert state.in_run_recovery.consecutive_failures == 1


def test_build_tool_result_error_events_use_standard_event_shape(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:test", turn_id="turn:test", reply_id="reply:test"
    )

    events = asyncio.run(
        runtime_session.emit_many(
            build_tool_result_error_events(
                context,
                tool_call_id="call:bad",
                tool_call_name="lookup",
                message="bad json",
            )
        )
    )

    assert [event.type for event in events] == [
        EventType.TOOL_RESULT_START,
        EventType.TOOL_RESULT_TEXT_DELTA,
        EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED,
        EventType.TOOL_RESULT_END,
    ]
    message = AssistantMsg(id="reply:test", name="assistant", content=[])
    reducer = MessageReducer(message)
    for event in events:
        reducer.append(event)
    assert message.content[0].state is ToolResultState.ERROR
    runtime_session.close()


class DenyGate:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def evaluate(self, calls):
        return PermissionDecision(kind=PermissionDecisionKind.DENY, reason=self.reason)


def test_permission_deny_reused_id_does_not_replay_prior_deny_reason(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    first_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:deny",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "x"}),
                    }
                ]
            },
            {"text": "first recovered"},
        ]
    )
    first_agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(first_transport),
        permission_gate=DenyGate("FIRST_DENY"),
    )
    asyncio.run(run_agent_task(first_agent, "deny first"))

    second_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:deny",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "x"}),
                    }
                ]
            },
            {"text": "second recovered"},
        ]
    )
    second_agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(second_transport),
        permission_gate=DenyGate("SECOND_DENY"),
    )

    second_result = asyncio.run(run_agent_task(second_agent, "deny second"))
    message_output = "\n".join(
        output.text
        for message in second_result.messages
        if message.role == "tool_result"
        for result in message.content
        if isinstance(result, ToolResultBlock)
        for output in result.output
        if isinstance(output, TextBlock)
    )
    second_context_text = "\n".join(
        text for msg in second_transport.contexts[1].messages for text in msg.content
    )

    assert "SECOND_DENY" in message_output
    assert "FIRST_DENY" not in message_output
    assert "SECOND_DENY" in second_context_text
    assert "FIRST_DENY" not in second_context_text


def test_terminal_policy_dangerous_command_requires_user_confirmation(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            }
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
    )

    result = asyncio.run(run_agent_task(agent, "attempt dangerous command"))
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    confirm = next(
        event for event in events if isinstance(event, RequireUserConfirmEvent)
    )

    assert result.status is LoopStatus.WAITING_USER
    assert result.stop_reason == "waiting_user"
    assert result.state.pending_tool_calls[0].id == "call:danger"
    assert result.state.pending_tool_calls[0].state is ToolCallState.ASKING
    assert confirm.tool_calls[0].id == "call:danger"
    assert confirm.tool_calls[0].name == "terminal"
    assert confirm.tool_calls[0].state is ToolCallState.ASKING
    assert confirm.tool_calls[0].suggested_rules[0]["reason"] == "terminal_access_ask"
    assert not any(isinstance(event, ToolResultStartEvent) for event in events)
    assert not any(isinstance(event, RunEndEvent) for event in events)


def test_agent_runtime_abort_run_finalizes_waiting_user_without_run_error(
    tmp_path,
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            }
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
    )

    first = asyncio.run(run_agent_task(agent, "attempt dangerous command"))
    result = asyncio.run(agent.abort_run(first.state))
    events = agent.runtime_session.event_log.iter(run_id=first.state.run_id)
    run_ends = [event for event in events if isinstance(event, RunEndEvent)]

    assert first.status is LoopStatus.WAITING_USER
    assert result.status is LoopStatus.ABORTED
    assert result.stop_reason == "aborted"
    assert result.state.pending_tool_calls == []
    assert [(event.status, event.stop_reason) for event in run_ends] == [
        ("aborted", "aborted")
    ]
    assert not any(isinstance(event, RunErrorEvent) for event in events)


def test_agent_runtime_finalize_run_is_idempotent(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "Say done"))
    second = asyncio.run(agent.abort_run(result.state))
    run_ends = [
        event
        for event in agent.runtime_session.event_log.iter(run_id=result.state.run_id)
        if isinstance(event, RunEndEvent)
    ]

    assert result.status is LoopStatus.FINISHED
    assert result.state.finalized is True
    assert second.status is LoopStatus.FINISHED
    assert [event.status for event in run_ends] == ["finished"]


def test_approval_resume_uses_original_run_snapshot_after_default_switch(
    tmp_path,
) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            },
            {"text": "continued"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("terminal", calls=calls))
    agent.tool_executor.registry = registry

    first = asyncio.run(run_agent_task(agent, "attempt dangerous command"))
    agent.set_permission_policy(preset_to_policy(PermissionMode.READ_ONLY))
    resolution = ApprovalResolution(
        approval_id="host-minted",
        decisions=(ToolApprovalDecision(tool_call_id="call:danger", confirmed=True),),
    )
    result = asyncio.run(agent.resume_after_approval(first.state, resolution))
    events = agent.runtime_session.event_log.iter(run_id=first.state.run_id)
    confirm_index = next(
        i for i, event in enumerate(events) if isinstance(event, UserConfirmResultEvent)
    )
    tool_start_index = next(
        i for i, event in enumerate(events) if isinstance(event, ToolResultStartEvent)
    )

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "continued"
    assert calls == ["call:danger"]
    assert confirm_index < tool_start_index
    assert [event.status for event in events if isinstance(event, RunEndEvent)] == [
        "finished"
    ]
    assert all(
        event.reply_id == first.state.messages[1].id
        for event in events
        if isinstance(
            event,
            (
                UserConfirmResultEvent,
                ToolResultStartEvent,
                ToolResultTextDeltaEvent,
                ToolResultEndEvent,
            ),
        )
    )
    assert len(transport.contexts) == 2
    assert "call:danger" in "\n".join(
        text for message in transport.contexts[1].messages for text in message.content
    )


def test_approval_resume_approved_call_does_not_reenter_permission_gate(
    tmp_path,
) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            },
            {"text": "continued"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("terminal", calls=calls))
    agent.tool_executor.registry = registry

    first = asyncio.run(run_agent_task(agent, "attempt dangerous command"))
    before_resume_confirm_count = sum(
        isinstance(event, RequireUserConfirmEvent)
        for event in agent.runtime_session.event_log.iter(run_id=first.state.run_id)
    )
    result = asyncio.run(
        agent.resume_after_approval(
            first.state,
            ApprovalResolution(
                approval_id="host-minted",
                decisions=(
                    ToolApprovalDecision(tool_call_id="call:danger", confirmed=True),
                ),
            ),
        )
    )
    after_resume_confirm_count = sum(
        isinstance(event, RequireUserConfirmEvent)
        for event in agent.runtime_session.event_log.iter(run_id=first.state.run_id)
    )

    assert result.status is LoopStatus.FINISHED
    assert calls == ["call:danger"]
    assert before_resume_confirm_count == 1
    assert after_resume_confirm_count == 1


def test_approval_resume_deny_returns_denied_tool_result_without_execution(
    tmp_path,
) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            },
            {"text": "denial acknowledged"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("terminal", calls=calls))
    agent.tool_executor.registry = registry

    first = asyncio.run(run_agent_task(agent, "attempt dangerous command"))
    result = asyncio.run(
        agent.resume_after_approval(
            first.state,
            ApprovalResolution(
                approval_id="host-minted",
                decisions=(
                    ToolApprovalDecision(tool_call_id="call:danger", confirmed=False),
                ),
            ),
        )
    )
    events = agent.runtime_session.event_log.iter(run_id=first.state.run_id)
    denied = next(event for event in events if isinstance(event, ToolResultEndEvent))
    second_context_text = "\n".join(
        text for message in transport.contexts[1].messages for text in message.content
    )

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "denial acknowledged"
    assert calls == []
    assert denied.state is ToolResultState.DENIED
    assert denied.render_profile.tool_origin == "terminal"
    assert denied.observation_timing.tool_origin == "terminal"
    assert isinstance(denied.essential_result, TerminalCommandErrorEssentialFact)
    assert denied.essential_capture_policy == (
        agent.tool_executor.essential_capture_policy
    )
    assert "tool call denied by user approval" in second_context_text


def test_approval_resume_defers_finalize_hooks_until_true_terminal_state(
    tmp_path,
) -> None:
    hooks = RecordingHooks()
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
        memory_hooks=hooks,
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("terminal", calls=[]))
    agent.tool_executor.registry = registry

    first = asyncio.run(run_agent_task(agent, "attempt dangerous command"))

    assert first.status is LoopStatus.WAITING_USER
    assert "turn_end" not in hooks.calls
    assert "end" not in hooks.calls

    result = asyncio.run(
        agent.resume_after_approval(
            first.state,
            ApprovalResolution(
                approval_id="host-minted",
                decisions=(
                    ToolApprovalDecision(tool_call_id="call:danger", confirmed=True),
                ),
            ),
        )
    )

    assert result.status is LoopStatus.FINISHED
    assert hooks.calls.count("turn_end") == 1
    assert hooks.calls.count("end") == 0


def test_approval_resume_partial_decisions_preserve_original_order(tmp_path) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:a",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build-a"}),
                    },
                    {
                        "id": "call:b",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build-b"}),
                    },
                    {
                        "id": "call:c",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build-c"}),
                    },
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("terminal", calls=calls))
    agent.tool_executor.registry = registry

    first = asyncio.run(run_agent_task(agent, "attempt dangerous commands"))
    result = asyncio.run(
        agent.resume_after_approval(
            first.state,
            ApprovalResolution(
                approval_id="host-minted",
                decisions=(
                    ToolApprovalDecision(tool_call_id="call:a", confirmed=True),
                    ToolApprovalDecision(tool_call_id="call:b", confirmed=False),
                    ToolApprovalDecision(tool_call_id="call:c", confirmed=True),
                ),
            ),
        )
    )
    result_ends = [
        event
        for event in agent.runtime_session.event_log.iter(run_id=first.state.run_id)
        if isinstance(event, ToolResultEndEvent)
    ]

    assert result.status is LoopStatus.FINISHED
    assert [(event.tool_call_id, event.state) for event in result_ends] == [
        ("call:a", ToolResultState.SUCCESS),
        ("call:b", ToolResultState.DENIED),
        ("call:c", ToolResultState.SUCCESS),
    ]
    assert calls == ["call:a", "call:c"]


def test_approval_resume_rejects_unknown_or_missing_decisions(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            }
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_ask_policy(),
    )

    first = asyncio.run(run_agent_task(agent, "attempt dangerous command"))

    with pytest.raises(ValueError, match="unknown tool calls"):
        asyncio.run(
            agent.resume_after_approval(
                first.state,
                ApprovalResolution(
                    approval_id="host-minted",
                    decisions=(
                        ToolApprovalDecision(tool_call_id="call:other", confirmed=True),
                    ),
                ),
            )
        )
    with pytest.raises(ValueError, match="missing decisions"):
        asyncio.run(
            agent.resume_after_approval(
                first.state, ApprovalResolution(approval_id="host-minted", decisions=())
            )
        )


def test_agent_runtime_finished_run_keeps_background_process_until_session_close(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bg",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "sleep 10", "yield_time_ms": 0}
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
        permission_policy=_terminal_bypass_policy(),
    )
    process_id: str | None = None

    try:
        result = asyncio.run(run_agent_task(agent, "start background then finish"))
        tool_delta = next(
            event
            for event in runtime_session.event_log.iter(run_id=result.state.run_id)
            if isinstance(event, ToolResultTextDeltaEvent)
            and event.tool_call_id == "call:bg"
        )
        process_id = json.loads(tool_delta.delta)["process_id"]

        assert result.status is LoopStatus.FINISHED
        assert (
            runtime_session.terminal_sessions.poll_process(process_id).status
            is TerminalStatus.RUNNING
        )
    finally:
        runtime_session.close()

    if process_id is not None:
        assert (
            runtime_session.terminal_sessions.poll_process(process_id).status
            is TerminalStatus.KILLED
        )


def test_tool_result_from_event_slice_folds_text_and_data_blocks() -> None:
    context = EventContext(
        run_id="run:slice", turn_id="turn:slice", reply_id="reply:slice"
    )
    events = [
        ToolResultStartEvent(
            **context.event_fields(), tool_call_id="call:slice", tool_call_name="lookup"
        ),
        ToolResultTextDeltaEvent(
            **context.event_fields(), tool_call_id="call:slice", delta="hello "
        ),
        ToolResultTextDeltaEvent(
            **context.event_fields(), tool_call_id="call:slice", delta="world"
        ),
        ToolResultDataDeltaEvent(
            **context.event_fields(),
            tool_call_id="call:slice",
            media_type="text/plain",
            data="abc",
        ),
        ToolResultDataDeltaEvent(
            **context.event_fields(),
            tool_call_id="call:slice",
            media_type="text/uri-list",
            url="https://example.test/result",
        ),
        ToolResultEndEvent(
            **context.event_fields(),
            **tool_result_end_contract_fields("call:slice", tool_name="lookup"),
            tool_call_id="call:slice",
            state=ToolResultState.SUCCESS,
            metadata={
                "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
            },
        ),
    ]

    block = _tool_result_from_event_slice(events, "call:slice")

    assert block.name == "lookup"
    assert block.state is ToolResultState.SUCCESS
    assert isinstance(block.output[0], TextBlock)
    assert block.output[0].text == "hello world"
    assert isinstance(block.output[1], DataBlock)
    assert isinstance(block.output[1].source, Base64Source)
    assert block.output[1].source.data == "abc"
    assert isinstance(block.output[2], DataBlock)
    assert block.output[2].source.url == "https://example.test/result"


def test_tool_result_from_event_slice_rejects_missing_or_malformed_slice() -> None:
    context = EventContext(
        run_id="run:slice", turn_id="turn:slice", reply_id="reply:slice"
    )

    try:
        _tool_result_from_event_slice([], "call:missing")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for missing tool result start")

    for events in [
        [
            ToolResultTextDeltaEvent(
                **context.event_fields(), tool_call_id="call:bad", delta="orphan"
            )
        ],
        [
            ToolResultEndEvent(
                **context.event_fields(),
                **tool_result_end_contract_fields(
                    "call:bad", tool_name="lookup", state=ToolResultState.ERROR
                ),
                tool_call_id="call:bad",
                state=ToolResultState.ERROR,
                metadata={
                    "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
                },
            )
        ],
        [
            ToolResultStartEvent(
                **context.event_fields(),
                tool_call_id="call:bad",
                tool_call_name="lookup",
            )
        ],
    ]:
        try:
            _tool_result_from_event_slice(events, "call:bad")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for malformed tool result slice")


class RecordingHooks(NoopMemoryHooks):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_session_start(self, state: LoopState, user_input: str) -> None:
        self.calls.append("start")

    async def on_turn_start(self, state: LoopState, user_input: str) -> None:
        self.calls.append("turn_start")

    async def project(self, state: LoopState, *, token_budget: int):
        self.calls.append("project")
        return {"summary": "Remember source=fenced.", "included_memory_ids": ["mem:1"]}

    async def after_model_reply(self, state: LoopState, assistant):
        self.calls.append("after_model")

    async def after_tool_results(self, state: LoopState, results):
        self.calls.append("after_tools")

    async def on_session_end(self, state: LoopState) -> None:
        self.calls.append("end")

    async def on_turn_end(self, state: LoopState) -> None:
        self.calls.append("turn_end")


class StaticMemoryScopePromptHooks(NoopMemoryHooks):
    def memory_context_prompt(self) -> str:
        return "MEMORY_SCOPE_INSTRUCTION_FROM_VERSIONED_FACT"


class CountingCapabilityProvider:
    def __init__(self, delegate: LocalSkillCapabilityProvider) -> None:
        self.delegate = delegate
        self.calls: list[CapabilityProjectionResolveContext] = []
        self.execution_surfaces: list[CapabilityExecutionSurfaceIdentityFact] = []
        self.provider_id = "counting-local-skills"

    def resolve_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        execution_surface,
    ):
        self.calls.append(context)
        self.execution_surfaces.append(execution_surface)
        return self.delegate.resolve_projection(
            context, execution_surface=execution_surface
        )


@dataclass(frozen=True, slots=True)
class StaticCapabilityProvider:
    descriptors: tuple[CapabilityDescriptor, ...]
    provider_id: str = "static-test"

    def snapshot_descriptors(
        self, context: CapabilityExecutionSurfaceSnapshotContext
    ) -> CapabilityDescriptorSnapshotOutput:
        del context
        return CapabilityDescriptorSnapshotOutput(descriptors=self.descriptors)


def _test_tool_descriptor(name: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=f"builtin:{name}",
        name=name,
        description=f"{name} test tool",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        namespace=None,
        provider_kind=CapabilityProviderKind.BUILTIN,
        provider_id="static-test",
        is_model_callable=True,
        is_read_only=True,
        is_concurrency_safe=True,
        result_render_contract=generic_result_render_contract(),
        long_horizon_policy=fixed_tool_action_policy(
            LongHorizonActionClass.EVIDENCE_ACQUISITION
        ),
        permission_category="general",
    )


_BUILTIN_TOOL_NAMES = frozenset(
    descriptor.name for descriptor in builtin_tool_descriptors()
)


def _install_registry_with_explicit_test_descriptors(
    agent: AgentRuntime, registry: ToolRegistry
) -> None:
    agent.tool_executor.registry = registry
    custom_names = tuple(sorted(set(registry.names()).difference(_BUILTIN_TOOL_NAMES)))
    if custom_names:
        agent.capability_runtime = CapabilityRuntime.with_default_providers(
            StaticCapabilityProvider(
                tuple(_test_tool_descriptor(name) for name in custom_names)
            )
        )


class SlowProjectionHooks(NoopMemoryHooks):
    async def project(self, state: LoopState, *, token_budget: int):
        await asyncio.sleep(0.05)
        return {"summary": "too late", "included_memory_ids": ["mem:late"]}


class SlowProjectionWithBaselineHooks(SlowProjectionHooks):
    def baseline_projection(self, state: LoopState, *, token_budget: int):
        return {
            "summary": (
                '<working-context-projection authority="recent_activity">'
                "PULSARA_RECENT_ACTIVITY_SURVIVES_TIMEOUT"
                "</working-context-projection>"
            ),
            "included_memory_ids": [],
            "projection_kind": "working_context",
        }


class ReadyThenFailedProjectionHooks(NoopMemoryHooks):
    def __init__(self) -> None:
        self.calls = 0

    async def project(self, state: LoopState, *, token_budget: int):
        del state, token_budget
        self.calls += 1
        if self.calls == 1:
            return {
                "summary": "STALE_MEMORY_MUST_NOT_RETURN",
                "included_memory_ids": ["memory:stale"],
            }
        raise RuntimeError("latest projection failed")


def test_memory_hooks_and_projection_events_are_used(tmp_path) -> None:
    hooks = RecordingHooks()
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=hooks,
    )

    asyncio.run(run_agent_task(agent, "hi"))

    assert hooks.calls == ["turn_start", "project", "after_model", "turn_end"]
    events = agent.runtime_session.event_log.iter()
    assert any(event.type is EventType.PROJECTION_REQUESTED for event in events)
    assert any(event.type is EventType.PROJECTION_READY for event in events)
    ready = next(event for event in events if event.type is EventType.PROJECTION_READY)
    context_text = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    assert "Recalled Memory" in context_text
    assert "Remember source=fenced." in context_text
    assert "[context timing: freshness=memory_projection;" in context_text
    compiled = next(
        event for event in events if isinstance(event, ContextCompiledEvent)
    )
    memory_section = next(
        section for section in compiled.sections if section["id"] == "memory:projection"
    )
    assert memory_section["metadata"]["source_timing"]["freshness"] == (
        "memory_projection"
    )
    assert memory_section["metadata"]["source_timing"]["clock_source"] == (
        "event_created_at"
    )
    assert (
        memory_section["metadata"]["source_timing"]["source_sequence_start"]
        == ready.sequence
    )
    assert memory_section["metadata"]["timing"]["source"]["freshness"] == (
        "memory_projection"
    )


def test_memory_hook_prompt_is_backed_by_versioned_static_fact(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    captured = []
    original_prepare = agent_module.prepare_live_context_snapshot

    async def capture_prepare(**kwargs):
        prepared = await original_prepare(**kwargs)
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(
        agent_module,
        "prepare_live_context_snapshot",
        capture_prepare,
    )
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=StaticMemoryScopePromptHooks(),
    )

    result = asyncio.run(run_agent_task(agent, "memory scope"))

    assert result.status is LoopStatus.FINISHED
    snapshot = captured[0].invocation.fact
    static = next(
        item
        for item in snapshot.static_instructions
        if item.source_id == "memory_scope_instruction"
    )
    authority = next(
        item
        for item in snapshot.candidate_authorities
        if item.source_instance_id == "memory:hook_prompt"
    )
    assert authority.source_artifact_ids == (static.content_artifact_id,)
    assert authority.lifecycle_dependency_fingerprint == static.fact_fingerprint
    assert authority.model_visible_text == (
        "MEMORY_SCOPE_INSTRUCTION_FROM_VERSIONED_FACT"
    )


def test_run_end_known_precommit_failure_retries_stable_candidate_once(
    tmp_path,
) -> None:
    class FailOnceRunEndLog(InMemoryEventLog):
        def __init__(self) -> None:
            super().__init__()
            self.run_end_attempts = 0

        def extend_with_materialization_state(
            self,
            events,
            *,
            expected_account_state_fingerprint,
            resulting_account_state,
            physical_charge_contract,
            expected_last_sequence,
            deadline_monotonic=None,
        ):
            batch = tuple(events)
            if any(isinstance(event, RunEndEvent) for event in batch):
                self.run_end_attempts += 1
                if self.run_end_attempts == 1:
                    raise RuntimeError("synthetic precommit RunEnd failure")
            return super().extend_with_materialization_state(
                batch,
                expected_account_state_fingerprint=(
                    expected_account_state_fingerprint
                ),
                resulting_account_state=resulting_account_state,
                physical_charge_contract=physical_charge_contract,
                expected_last_sequence=expected_last_sequence,
                deadline_monotonic=deadline_monotonic,
            )

    event_log = FailOnceRunEndLog()
    runtime_session = in_memory_runtime_session(tmp_path, event_log=event_log)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    state = agent.new_state()

    result = asyncio.run(run_agent_task(agent, "finish", state=state))

    assert result.status is LoopStatus.FINISHED
    assert state.finalized is True
    assert state.scratchpad["run_end_commit_state"] == "committed"
    assert event_log.run_end_attempts == 2
    terminal = [event for event in event_log.iter() if isinstance(event, RunEndEvent)]
    assert len(terminal) == 1
    assert terminal[0].id == state.scratchpad["terminal_run_end_event_id"]


def test_run_end_postcommit_publication_failure_folds_committed_terminal(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    class FailRunEndObserver:
        async def on_published_event(self, published: RuntimePublishedEvent) -> None:
            if isinstance(published.event, RunEndEvent):
                raise RuntimeError("synthetic RunEnd observer failure")

    runtime_session.publisher.subscribe(FailRunEndObserver())
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    state = agent.new_state()

    with pytest.raises(EventPublicationAfterCommitError):
        asyncio.run(run_agent_task(agent, "finish", state=state))

    terminal = [
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, RunEndEvent)
    ]
    assert len(terminal) == 1
    assert state.finalized is True
    assert state.scratchpad["run_end_commit_state"] == "committed"


def test_capability_runtime_resolves_once_per_user_message_and_exposure_is_stable(
    tmp_path,
) -> None:
    _write_workspace_skill(
        tmp_path,
        "review-pr",
        """---
name: review-pr
description: Review pull requests.
provides_tools:
  - noop
---
# Review PR

Use the review checklist.
""",
    )
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:noop", "name": "noop", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key=str(tmp_path),
    )
    provider = CountingCapabilityProvider(_workspace_only_capability_provider())
    agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
        capability_runtime=CapabilityRuntime.with_default_providers(
            StaticCapabilityProvider((_test_tool_descriptor("noop"),)),
            provider,
        ),
        memory_domain=domain,
        workspace_kind="project",
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("noop", calls=[]))
    agent.tool_executor.registry = registry

    result = asyncio.run(run_agent_task(agent, "$review-pr inspect this"))

    assert result.status is LoopStatus.FINISHED
    assert len(provider.calls) == 1
    assert provider.calls[0].workspace_root == tmp_path
    assert provider.calls[0].workspace_kind == "project"
    assert provider.calls[0].memory_domain == domain
    assert {
        entry.capability_name for entry in provider.execution_surfaces[0].entries
    } == {"noop"}
    assert len(transport.contexts) == 2
    assert _without_context_timing_lines(
        transport.contexts[0].system_prompt or ""
    ) == _without_context_timing_lines(transport.contexts[1].system_prompt or "")
    first_context_text = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    assert "Available Skills:" in first_context_text
    assert "Active Skill: review-pr" in (transport.contexts[0].system_prompt or "")
    assert "# Review PR" in (transport.contexts[0].system_prompt or "")
    assert [
        [tool.name for tool in context.tools] for context in transport.contexts
    ] == [["noop"], ["noop"]]


def test_agent_runtime_accepts_host_selected_active_skill(tmp_path) -> None:
    _write_workspace_skill(
        tmp_path,
        "review-pr",
        """---
name: review-pr
description: Review pull requests.
---
# Review PR
""",
    )
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        capability_runtime=CapabilityRuntime.with_default_providers(
            _workspace_only_capability_provider()
        ),
    )

    result = asyncio.run(
        run_agent_task(
            agent, "inspect this", active_skill_names=frozenset({"review-pr"})
        )
    )

    assert result.status is LoopStatus.FINISHED
    assert "Active Skill: review-pr" in (transport.contexts[0].system_prompt or "")
    assert "Reason: host_command" in (transport.contexts[0].system_prompt or "")
    assert "# Review PR" in (transport.contexts[0].system_prompt or "")


def _write_workspace_skill(root, name: str, content: str) -> None:
    skill_dir = root / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _workspace_only_capability_provider() -> LocalSkillCapabilityProvider:
    return LocalSkillCapabilityProvider(
        provider=LocalSkillProvider(include_user_skills=False)
    )


def test_memory_projection_timeout_fails_soft_without_blocking_reply(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=SlowProjectionHooks(),
        budget=LoopBudget(recall_hard_timeout_ms=1),
    )

    result = asyncio.run(run_agent_task(agent, "hi"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "done"
    assert result.state.memory_projection is None
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    failed = next(
        event for event in events if event.type is EventType.PROJECTION_FAILED
    )
    assert failed.error == "recall_timeout"
    assert "Recalled Memory" not in (transport.contexts[0].system_prompt or "")


def test_latest_failed_memory_projection_does_not_reuse_prior_ready(tmp_path) -> None:
    hooks = ReadyThenFailedProjectionHooks()
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:missing",
                        "name": "missing_tool",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=hooks,
    )

    result = asyncio.run(run_agent_task(agent, "use memory once"))

    assert result.status is LoopStatus.FINISHED
    assert hooks.calls == 2
    first_context = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    second_context = "\n".join(
        text for message in transport.contexts[1].messages for text in message.content
    )
    assert "STALE_MEMORY_MUST_NOT_RETURN" in first_context
    assert "STALE_MEMORY_MUST_NOT_RETURN" not in second_context
    event_types = [
        event.type
        for event in agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    ]
    assert event_types.count(EventType.PROJECTION_REQUESTED) == 2
    assert event_types.count(EventType.PROJECTION_READY) == 1
    assert event_types.count(EventType.PROJECTION_FAILED) == 1


def test_zero_subagent_cap_persists_omitted_only_selection_audit(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    captured = []
    original_prepare = agent_module.prepare_live_context_snapshot

    async def capture_prepare(**kwargs):
        prepared = await original_prepare(**kwargs)
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(
        agent_module,
        "prepare_live_context_snapshot",
        capture_prepare,
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
        budget=LoopBudget(max_subagent_results_per_parent_compile=0),
    )
    assert agent.subagent_runtime is not None
    # This test seeds a historical graph fixture without opening a production
    # rollout account. Keep that fixture on the explicit fake-graph path; live
    # spawn tools remain bound to the required rollout admission contract.
    agent.subagent_runtime.bind_rollout_admission(None)
    agent.subagent_runtime.bind_rollout_terminal_augmenter(None)

    async def exercise():
        seed_context = EventContext(
            run_id="run:selection-seed",
            turn_id="turn:selection-seed",
            reply_id="reply:selection-seed",
        )
        await runtime_session.emit(
            RunStartEvent(
                **seed_context.event_fields(),
                **run_start_permission_fields(
                    seed_context.run_id,
                    turn_id=seed_context.turn_id,
                    reply_id=seed_context.reply_id,
                    mcp_installation_owner_runtime_session_id=(
                        runtime_session.runtime_session_id
                    ),
                ),
                user_input_chars=0,
            )
        )
        seeded = await agent.subagent_runtime.spawn_fake(
            task="omitted result",
            event_context=seed_context,
        )
        await agent.subagent_runtime.complete_fake(
            seeded.subagent_run_id,
            summary="pending but capped",
            event_context=seed_context,
        )
        return await run_agent_task(agent, "compile with zero subagent cap")

    result = asyncio.run(exercise())

    assert result.status is LoopStatus.FINISHED
    prepared = captured[0]
    selection = prepared.invocation.fact.candidate_source_selections[0]
    assert selection.source_instance_id == "subagent:results"
    assert selection.eligible_source_count == 1
    assert selection.selected_source_ids == ()
    assert selection.omitted_source_count == 1
    assert selection.reason_code == "policy_limit"
    assert not any(
        item.source_instance_id == "subagent:results"
        for item in prepared.invocation.fact.candidate_authorities
    )
    decision = next(
        item
        for item in prepared.prepared_candidates.collection_decisions
        if item.source_kind == "subagent_results"
    )
    assert decision.selected_source_ids == ()
    assert decision.omitted_source_count == 1
    assert decision.reason_code == "policy_limit"
    compiled = next(
        event
        for event in runtime_session.event_log.iter(run_id=result.state.run_id)
        if isinstance(event, ContextCompiledEvent) and event.status == "compiled"
    )
    assert compiled.input_audit is not None
    manifest = load_context_input_manifest(
        audit=compiled.input_audit,
        archive=runtime_session.archive,
    )
    assert manifest.snapshot.candidate_source_selections == (selection,)
    assert decision in manifest.prepared_candidate_set.collection_decisions


def test_memory_projection_timeout_preserves_working_context_baseline(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=SlowProjectionWithBaselineHooks(),
        budget=LoopBudget(recall_hard_timeout_ms=1),
    )

    result = asyncio.run(run_agent_task(agent, "What did I just do?"))

    assert result.status is LoopStatus.FINISHED
    assert result.state.memory_projection is not None
    assert (
        "PULSARA_RECENT_ACTIVITY_SURVIVES_TIMEOUT"
        in result.state.memory_projection["summary"]
    )
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    ready = next(event for event in events if event.type is EventType.PROJECTION_READY)
    assert ready.metadata == {
        "degraded": True,
        "warnings": ["semantic_recall_timeout"],
        "fallback": "baseline_projection",
    }
    assert not any(event.type is EventType.PROJECTION_FAILED for event in events)
    context_text = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    assert "PULSARA_RECENT_ACTIVITY_SURVIVES_TIMEOUT" in context_text
    assert "empty memory_search result does not invalidate" in context_text


class FailingHook(NoopMemoryHooks):
    def __init__(self, hook_name: str) -> None:
        self.hook_name = hook_name

    def _maybe_raise(self, hook_name: str) -> None:
        if self.hook_name == hook_name:
            raise RuntimeError(f"{hook_name} boom")

    async def on_session_start(self, state: LoopState, user_input: str) -> None:
        self._maybe_raise("on_session_start")

    async def after_model_reply(self, state: LoopState, assistant) -> None:
        self._maybe_raise("after_model_reply")

    async def after_tool_results(self, state: LoopState, results) -> None:
        self._maybe_raise("after_tool_results")

    async def should_compact(self, state: LoopState) -> bool:
        self._maybe_raise("should_compact")
        return False

    async def on_session_end(self, state: LoopState) -> None:
        self._maybe_raise("on_session_end")


class InvalidEventHook(NoopMemoryHooks):
    async def after_model_reply(self, state: LoopState, assistant) -> list[AgentEvent]:
        return [
            make_text_block_segment_event(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                block_id="text:invalid",
                delta="invalid",
                sequence=99,
            )
        ]


class LegacyShapeMemoryHook:
    async def on_session_start(self, state: LoopState, user_input: str) -> None:
        return None

    async def project(self, state: LoopState, *, token_budget: int):
        return None

    async def after_model_reply(self, state: LoopState, assistant):
        return []

    async def after_tool_results(self, state: LoopState, results):
        return []

    async def should_compact(self, state: LoopState) -> bool:
        return False

    async def on_session_end(self, state: LoopState):
        return []


class FailingPersistenceHook:
    async def after_tool_results(
        self, state: LoopState, results: list[ToolResultBlock]
    ) -> None:
        raise RuntimeError("persist boom")


def _assert_memory_hook_failed(agent: AgentRuntime, result, hook_name: str) -> None:
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    error = next(event for event in events if isinstance(event, RunErrorEvent))
    completed = next(event for event in events if isinstance(event, RunEndEvent))

    assert result.status is LoopStatus.FAILED
    assert result.stop_reason == "memory_hook_error"
    assert hook_name in (result.error_message or "")
    assert error.code == "memory_hook_error"
    assert error.metadata == {"hook": hook_name}
    assert completed.status == "failed"
    assert completed.stop_reason == "memory_hook_error"
    assert hook_name in (completed.error_message or "")


def test_memory_hook_failure_on_session_start_returns_failed_result(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "should not run"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("on_session_start"),
    )

    result = asyncio.run(run_agent_task(agent, "hi"))

    _assert_memory_hook_failed(agent, result, "on_turn_start")
    assert "on_session_start boom" in (result.error_message or "")
    assert transport.contexts == []


def test_memory_hook_failure_after_model_reply_returns_failed_result(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("after_model_reply"),
    )

    result = asyncio.run(run_agent_task(agent, "hi"))

    _assert_memory_hook_failed(agent, result, "after_model_reply")
    assert result.final_text == "done"


def test_memory_hook_event_emit_failure_returns_failed_result(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=InvalidEventHook(),
    )

    result = asyncio.run(run_agent_task(agent, "hi"))

    _assert_memory_hook_failed(agent, result, "after_model_reply")
    assert result.final_text == "done"


def test_agent_runtime_accepts_memory_hook_without_proposal_sink_property(
    tmp_path,
) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=LegacyShapeMemoryHook(),
    )

    result = asyncio.run(run_agent_task(agent, "hi"))

    assert result.status is LoopStatus.FINISHED
    assert "propose_memory" not in agent.tool_executor.registry.names()
    assert not any(
        name.startswith("remember_") for name in agent.tool_executor.registry.names()
    )


def test_memory_hook_failure_after_tool_results_returns_failed_result(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "should not run"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("after_tool_results"),
    )

    result = asyncio.run(run_agent_task(agent, "read"))

    _assert_memory_hook_failed(agent, result, "after_tool_results")
    assert len(transport.contexts) == 1


def test_tool_result_persistence_hook_records_runtime_facts_only(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    graph = InMemoryGraphStore()
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        tool_result_persistence_hook=ExecutionEvidencePersistenceHook(ledger),
    )

    result = asyncio.run(run_agent_task(agent, "read"))

    assert result.status is LoopStatus.FINISHED
    tool_results = graph.find_by_type(rt.TOOL_RESULT)
    assert len(tool_results) == 1
    assert graph.find_by_type(rt.EVIDENCE) == []
    assert graph.find_by_type(memory.CLAIM) == []
    span = tool_results[0][rt.EVENT_SPAN_PROPERTY.name]
    assert span[rt.SOURCE_SESSION.name] == agent.runtime_session.runtime_session_id


def test_tool_result_persistence_hook_rejects_large_external_result_without_artifact_ref(
    tmp_path,
) -> None:
    graph = InMemoryGraphStore()
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    hook = ExecutionEvidencePersistenceHook(ledger)
    state = LoopState(session_id="runtime:test")
    state.current_scope = "ctx:workspace/test_project"
    state.pending_tool_calls = [
        ToolCallBlock(
            id="call:external", name="external_tool", input='{"mode":"external"}'
        )
    ]
    result = ToolResultBlock(
        id="call:external",
        name="external_tool",
        output=[TextBlock(text="x" * 8_100)],
        state=ToolResultState.SUCCESS,
    )

    with pytest.raises(ValueError, match="but no artifact ref"):
        asyncio.run(hook.after_tool_results(state, [result]))

    assert graph.find_by_type(rt.TOOL_RESULT) == []
    assert graph.find_by_type(rt.ARTIFACT) == []


def test_tool_result_persistence_hook_accepts_large_artifact_read_with_source_ref(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    artifact_id = "artifact:tool-result:run-source:call-large:output:0"
    write = runtime_session.archive.put_text(
        artifact_id,
        "SOURCE_HEAD\n" + ("x" * 12_000) + "\nSOURCE_TAIL",
        session_id=runtime_session.runtime_session_id,
        run_id="run:source",
        media_type="text/plain; charset=utf-8",
    )
    runtime_session.tool_result_artifacts.put(
        ToolResultArtifactRecord(
            id="tool-result-artifact:run-source:call-large:output:0",
            session_id=runtime_session.runtime_session_id,
            run_id="run:source",
            turn_id="turn:source",
            reply_id="reply:source",
            tool_call_id="call:large",
            tool_name="terminal",
            artifact_id=write.id,
            role="output",
            ordinal=0,
            media_type="text/plain; charset=utf-8",
            size_bytes=write.size_bytes,
        )
    )
    context = EventContext(
        run_id="run:read", turn_id="turn:read", reply_id="reply:read"
    )
    executor = runtime_session.create_tool_executor(
        record_event=runtime_session.make_thread_recorder()
    )
    result = executor.execute(
        ToolCall(
            id="call:artifact-read",
            name="artifact_read",
            arguments={"artifact_id": artifact_id, "max_chars": 20_000},
        ),
        event_context=context,
    )
    prepared_terminal = result.prepared_terminal_result
    assert prepared_terminal is not None
    asyncio.run(
        runtime_session.emit_many(
            (
                build_tool_result_terminal_event(
                    event_context=context,
                    prepared=prepared_terminal,
                ),
            )
        )
    )
    replayed = AssistantMsg(name="assistant", id="reply:read", content=[])
    reducer = MessageReducer(replayed)
    for event in runtime_session.event_log.iter():
        if event.reply_id == "reply:read":
            reducer.append(event)
    block = replayed.content[0]
    assert result.status is ToolResultState.SUCCESS
    assert isinstance(block, ToolResultBlock)
    assert block.artifacts and block.artifacts[0].artifact_id == artifact_id

    graph = InMemoryGraphStore()
    hook = ExecutionEvidencePersistenceHook(
        ExecutionEvidenceLedger(
            graph=graph,
            archive=runtime_session.archive,
            gate=MemoryWriteGate(),
        )
    )
    state = LoopState(
        session_id=runtime_session.runtime_session_id, turn_id="turn:read"
    )
    state.current_scope = "ctx:workspace/test_project"
    state.pending_tool_calls = [
        ToolCallBlock(
            id="call:artifact-read",
            name="artifact_read",
            input=json.dumps({"artifact_id": artifact_id}),
        )
    ]

    asyncio.run(hook.after_tool_results(state, [block]))

    assert graph.find_by_type(rt.TOOL_RESULT)
    assert graph.find_by_type(rt.ARTIFACT)


def test_terminal_large_output_followup_context_preserves_exact_artifact_id(
    tmp_path,
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:large-terminal",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    'python -c \'print("PULSARA_HEAD"); '
                                    'print("q" * 50000); '
                                    'print("PULSARA_TAIL")\''
                                ),
                                "max_output_chars": 120,
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )

    result = asyncio.run(run_agent_task(agent, "run large terminal output"))

    assert result.status is LoopStatus.FINISHED
    terminal_end = next(
        event
        for event in agent.runtime_session.event_log.iter(run_id=result.state.run_id)
        if isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:large-terminal"
    )
    artifact_id = terminal_end.artifacts[0].artifact_id
    followup_text = "\n".join(
        text for message in transport.contexts[1].messages for text in message.content
    )
    assert artifact_id in followup_text


def test_tool_result_persistence_hook_failure_does_not_break_run(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        tool_result_persistence_hook=FailingPersistenceHook(),
    )

    result = asyncio.run(run_agent_task(agent, "read"))
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)

    assert result.status is LoopStatus.FINISHED
    assert result.stop_reason == "final"
    assert any(
        event.type is EventType.CUSTOM
        and event.name == "tool_result_persistence_failed"
        for event in events
    )
    assert not any(
        isinstance(event, RunErrorEvent) and event.code == "memory_persistence_error"
        for event in events
    )
    assert any(
        isinstance(event, RunEndEvent) and event.status == "finished"
        for event in events
    )


def test_memory_hook_failure_should_compact_returns_failed_result(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:read",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "note.txt"}),
                    }
                ]
            },
            {"text": "should not run"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("should_compact"),
    )

    result = asyncio.run(run_agent_task(agent, "read"))

    _assert_memory_hook_failed(agent, result, "should_compact")
    assert len(transport.contexts) == 1


def test_memory_hook_failure_on_session_end_returns_failed_result(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("on_session_end"),
    )

    result = asyncio.run(run_agent_task(agent, "hi"))

    _assert_memory_hook_failed(agent, result, "on_turn_end")
    assert "on_session_end boom" in (result.error_message or "")
    assert result.final_text == "done"


@dataclass(slots=True)
class SyncConcurrencyProbe:
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    max_active: int = 0


@dataclass(slots=True)
class SleepTool:
    name: str
    delay: float
    probe: SyncConcurrencyProbe | None = None
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    description: str = "Sleep briefly."
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        if self.probe is not None:
            with self.probe.lock:
                self.probe.active += 1
                self.probe.max_active = max(
                    self.probe.max_active,
                    self.probe.active,
                )
        try:
            time.sleep(self.delay)
        finally:
            if self.probe is not None:
                with self.probe.lock:
                    self.probe.active -= 1
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=call.name,
        )


@dataclass(slots=True)
class AsyncConcurrencyProbeTool:
    name: str
    shared: dict[str, object]
    delay: float = 0.05
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    description: str = "Probe native async tool concurrency."
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        active = int(self.shared.get("active", 0)) + 1
        self.shared["active"] = active
        self.shared["max_active"] = max(int(self.shared.get("max_active", 0)), active)
        self.shared.setdefault("contexts", []).append(runtime_context)  # type: ignore[union-attr]
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.shared["active"] = int(self.shared["active"]) - 1
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=call.name,
        )


class _ConcurrentRecallService:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.queries = []

    async def recall(self, query, *, graph_id=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.queries.append(query)
        try:
            await asyncio.sleep(0.05)
            return RecallResult(status=RecallStatus.EMPTY)
        finally:
            self.active -= 1


@dataclass(slots=True)
class RecordingTool:
    name: str
    calls: list[str]
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    description: str = "Record execution."
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        self.calls.append(call.id)
        if self.name == "terminal":
            timing = build_terminal_payload_timing(
                observed_at_utc="2026-01-01T00:00:00Z",
                duration_seconds=0,
                freshness="current_tool_observation",
                clock_source="tool_runtime_metadata",
            )
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.SUCCESS,
                output=call.id,
                semantics_input=FrozenToolResultSemanticsRuntimeInput(
                    semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_COMMAND_EXECUTED,
                    domain_submission=TerminalCommandDomainSubmissionFact(
                        command=str(call.arguments.get("command") or "test command"),
                        status="success",
                        exit_code=0,
                        cwd="/test",
                        timed_out=False,
                        output_truncated=False,
                        error=None,
                        process_id=None,
                        yielded_to_background=False,
                        terminal_session_id="test",
                        backend_type="test",
                        io_mode=None,
                        stdin_closed=None,
                        policy_code=None,
                        duration_seconds=0,
                    ),
                ),
                terminal_payload_timing=timing,
            )
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=call.id,
        )


def test_per_batch_executor_preserves_frozen_capture_policy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.runtime.agent as agent_module

    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(RecordingTool(name="capture_probe", calls=calls))
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:capture",
                        "name": "capture_probe",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)
    policy_payload = {
        "policy_version": "capture:test-non-default",
        "max_error_chars": 17,
        "max_process_summaries": 2,
        "max_process_command_chars": 31,
        "max_process_cwd_chars": 29,
    }
    policy = ToolResultEssentialCapturePolicyFact(
        **policy_payload,
        policy_fingerprint=context_fingerprint(
            "tool-result-essential-capture-policy:v1",
            policy_payload,
        ),
    )
    agent.tool_executor.essential_capture_policy = policy
    real_executor = agent_module.ToolExecutor
    captured: list[ToolResultEssentialCapturePolicyFact] = []

    def capture_executor(**kwargs):
        captured.append(kwargs["essential_capture_policy"])
        return real_executor(**kwargs)

    monkeypatch.setattr(agent_module, "ToolExecutor", capture_executor)

    result = asyncio.run(run_agent_task(agent, "exercise capture policy"))

    assert result.status is LoopStatus.FINISHED
    assert calls == ["call:capture"]
    assert captured == [policy]


@dataclass(slots=True)
class BlockingUntilStartHookTool:
    release: threading.Event
    name: str = "blocking_tool"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    description: str = "Wait until the start hook releases execution."
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        released = self.release.wait(timeout=0.5)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS if released else ToolResultState.ERROR,
            output="released" if released else "not released before start hook",
        )


def test_tool_result_start_hook_dispatches_before_tool_finishes(tmp_path) -> None:
    release = threading.Event()
    runtime_session = in_memory_runtime_session(tmp_path)
    registry = ToolRegistry()
    registry.register(BlockingUntilStartHookTool(release=release))
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:block", "name": "blocking_tool", "arguments": "{}"}
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)

    def release_on_start(context, event) -> None:
        if (
            isinstance(event, ToolResultStartEvent)
            and event.tool_call_id == "call:block"
        ):
            release.set()

    runtime_session.hook_manager.register_event(
        EventType.TOOL_RESULT_START, release_on_start
    )

    result = asyncio.run(run_agent_task(agent, "run blocking tool"))

    assert result.status is LoopStatus.FINISHED
    tool_output = "\n".join(
        output.text
        for message in result.messages
        if message.role == "tool_result"
        for block in message.content
        if isinstance(block, ToolResultBlock)
        for output in block.output
        if isinstance(output, TextBlock)
    )
    assert "released" in tool_output
    assert "not released" not in tool_output


def test_cancelled_tool_batch_waits_for_sync_worker_before_releasing_borrow(
    tmp_path,
) -> None:
    started = threading.Event()
    release_worker = threading.Event()

    @dataclass(slots=True)
    class BlockingSyncTool:
        name: str = "blocking_sync_tool"
        description: str = "Block in a real worker thread."
        parameters: dict = field(
            default_factory=lambda: {"type": "object", "properties": {}}
        )
        is_read_only: bool = True
        is_concurrency_safe: bool = True

        def execute(self, call: ToolCall) -> ToolExecutionResult:
            started.set()
            release_worker.wait()
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.SUCCESS,
                output="done",
            )

    runtime_session = in_memory_runtime_session(tmp_path)
    registry = ToolRegistry()
    registry.register(BlockingSyncTool())
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "unused"}])),
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)
    descriptor = _test_tool_descriptor("blocking_sync_tool")
    exposure = CapabilityExposurePlan(
        registry_generation=1,
        direct_tool_specs=(),
        direct_names=frozenset({descriptor.name}),
        deferred_names=frozenset(),
        hidden_names=frozenset(),
        callable_names=frozenset({descriptor.name}),
        descriptors_by_name={descriptor.name: descriptor},
        catalog_entries=(),
        active_injections=(),
        catalog_prompt=None,
        active_skill_prompt=None,
        diagnostics=(),
    )
    state = agent.new_state()
    state.run_model_target = agent.resolve_run_model_target()
    state.permission_snapshot = agent._capture_run_permission_snapshot(state)
    descriptor_fp = descriptor.fingerprint()
    synthetic_surface = SimpleNamespace(
        entries=(
            SimpleNamespace(
                descriptor_id=descriptor.id,
                descriptor_fingerprint=descriptor_fp,
            ),
        ),
        descriptor_set_fingerprint="descriptor-set:sync-tool-test",
    )
    synthetic_exposure_fact = SimpleNamespace(
        exposure_id="capability-exposure:sync-tool-test",
        exposure_fact_fingerprint="exposure-fact:sync-tool-test",
        semantic=SimpleNamespace(execution_surface=synthetic_surface),
    )
    exposure_event_ref = ContextEventReferenceFact(
        runtime_session_id=runtime_session.runtime_session_id,
        event_id="capability-exposure-event:sync-tool-test",
        sequence=1,
        event_type="capability_exposure_resolved",
        payload_fingerprint="sha256:" + "0" * 64,
    )
    state.run_working_set = RunWorkingSet(
        run_start_event_id="run-start:sync-tool-test",
        run_start_sequence=1,
        run_model_target=state.run_model_target,
        long_horizon_contract=object(),  # type: ignore[arg-type]
        run_transcript_seed_semantic=object(),  # type: ignore[arg-type]
        run_transcript_seed_reference=object(),  # type: ignore[arg-type]
        permission_snapshot=state.permission_snapshot,
        plan_snapshot=object(),  # type: ignore[arg-type]
        capability_resolve_basis=object(),  # type: ignore[arg-type]
        frozen_execution_surface=object(),  # type: ignore[arg-type]
        original_exposure_plan=exposure,
        original_exposure_fact=synthetic_exposure_fact,  # type: ignore[arg-type]
        original_exposure_event_ref=exposure_event_ref,
        effective_exposure_plan=exposure,
        effective_exposure_fact=synthetic_exposure_fact,  # type: ignore[arg-type]
        effective_exposure_event_ref=exposure_event_ref,
        latest_committed_resume_boundary=None,
        latest_committed_resume_boundary_ref=None,
    )
    handles = BoundaryExecutionHandles(
        handle_id="handles:sync-tool-test",
        handle_generation=1,
        owner_id=state.run_id,
        state="run_owned",
        mcp_installation=object(),
        capability_runtime=agent.capability_runtime,
        tool_registry=registry,
        frozen_execution_surface=object(),  # type: ignore[arg-type]
    )
    state.scratchpad["capability_execution_borrow_authority"] = handles.borrow_authority

    async def scenario() -> None:
        async def consume() -> None:
            async for _event in agent._stream_tool_batch_events(
                state,
                [
                    ToolCall(
                        id="call:blocking-sync",
                        name="blocking_sync_tool",
                        arguments={},
                    )
                ],
                [],
                exposure=exposure,
                reservations={},
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.to_thread(started.wait)
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert handles.borrow_tracker.active_parent_tool_call_borrows == 1

        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handles.borrow_tracker.active_parent_tool_call_borrows == 0

    asyncio.run(scenario())


def test_tool_result_and_reservation_settlement_commit_atomically(tmp_path) -> None:
    started = threading.Event()
    release_worker = threading.Event()

    @dataclass(slots=True)
    class BlockingSyncTool:
        name: str = "atomic_terminal_tool"
        description: str = "Complete only after the cancellation boundary is held."
        parameters: dict = field(
            default_factory=lambda: {"type": "object", "properties": {}}
        )
        is_read_only: bool = True
        is_concurrency_safe: bool = True

        def execute(self, call: ToolCall) -> ToolExecutionResult:
            started.set()
            release_worker.wait()
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.SUCCESS,
                output="physically completed",
            )

    runtime_session = in_memory_runtime_session(tmp_path)
    registry = ToolRegistry()
    registry.register(BlockingSyncTool())
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(
            ScriptedTransport(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "call:atomic-terminal",
                                "name": "atomic_terminal_tool",
                                "arguments": "{}",
                            }
                        ]
                    }
                ]
            )
        ),
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)
    state = agent.new_state()

    async def scenario() -> None:
        async def consume() -> None:
            async for _event in stream_agent_task(
                agent,
                "run the blocking tool",
                state=state,
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.to_thread(started.wait)
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert runtime_session.tool_execution_terminal_registry.active_owner_count() == 1

        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    terminal = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:atomic-terminal"
    )
    settlement = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.source_tool_result_event_id == terminal.id
    )
    assert terminal.state is ToolResultState.SUCCESS
    assert settlement.sequence == terminal.sequence + 1
    assert runtime_session.tool_execution_terminal_registry.active_owner_count() == 0


def test_async_tool_cancellation_settles_interrupted_terminal(tmp_path) -> None:
    started = asyncio.Event()

    @dataclass(slots=True)
    class BlockingAsyncTool:
        name: str = "interruptible_async_tool"
        description: str = "Wait until the owning run requests cancellation."
        parameters: dict = field(
            default_factory=lambda: {"type": "object", "properties": {}}
        )
        is_read_only: bool = True
        is_concurrency_safe: bool = True

        async def execute_async(
            self,
            call: ToolCall,
            *,
            runtime_context: ToolRuntimeContext,
        ) -> ToolExecutionResult:
            del call, runtime_context
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    runtime_session = in_memory_runtime_session(tmp_path)
    registry = ToolRegistry()
    registry.register(BlockingAsyncTool())
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(
            ScriptedTransport(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "call:async-interrupted",
                                "name": "interruptible_async_tool",
                                "arguments": "{}",
                            }
                        ]
                    }
                ]
            )
        ),
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)
    state = agent.new_state()

    async def scenario() -> None:
        async def consume() -> None:
            async for _event in stream_agent_task(
                agent,
                "run the async tool",
                state=state,
            ):
                pass

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    terminal = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:async-interrupted"
    )
    settlement = next(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, RolloutBudgetReservationSettledEvent)
        and event.source_tool_result_event_id == terminal.id
    )
    assert terminal.state is ToolResultState.INTERRUPTED
    assert settlement.sequence == terminal.sequence + 1
    assert runtime_session.tool_execution_terminal_registry.active_owner_count() == 0


def test_duplicate_tool_call_id_marks_provider_call_audit_only_without_execution(
    tmp_path,
) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:dup", "name": "dup_tool", "arguments": "{}"},
                    {"id": "call:dup", "name": "dup_tool", "arguments": "{}"},
                ]
            },
            {"text": "recovered"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )
    registry = ToolRegistry()
    registry.register(
        RecordingTool(
            "dup_tool", calls=calls, is_read_only=True, is_concurrency_safe=True
        )
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)

    result = asyncio.run(run_agent_task(agent, "run duplicate tool ids"))
    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "recovered"
    assert calls == []
    assert any(
        isinstance(event, ProviderModelStreamErrorEvent)
        for event in agent.runtime_session.event_log.iter()
    )
    assert not any(
        isinstance(event, ToolResultEndEvent) and event.tool_call_id == "call:dup"
        for event in agent.runtime_session.event_log.iter()
    )


def test_duplicate_tool_call_id_suppresses_the_entire_provider_tool_batch(
    tmp_path,
) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:ok", "name": "ok_tool", "arguments": "{}"},
                    {"id": "call:dup", "name": "dup_tool", "arguments": "{}"},
                    {"id": "call:dup", "name": "dup_tool", "arguments": "{}"},
                ]
            },
            {"text": "recovered"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )
    registry = ToolRegistry()
    registry.register(
        RecordingTool(
            "ok_tool", calls=calls, is_read_only=True, is_concurrency_safe=True
        )
    )
    registry.register(
        RecordingTool(
            "dup_tool", calls=calls, is_read_only=True, is_concurrency_safe=True
        )
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)

    result = asyncio.run(run_agent_task(agent, "run mixed duplicate tool ids"))
    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "recovered"
    assert calls == []
    assert any(
        isinstance(event, ProviderModelStreamErrorEvent)
        for event in agent.runtime_session.event_log.iter()
    )
    assert not any(
        isinstance(event, ToolResultEndEvent)
        and event.tool_call_id in {"call:ok", "call:dup"}
        for event in agent.runtime_session.event_log.iter()
    )


def test_readonly_concurrency_safe_tools_run_concurrently(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:a", "name": "sleep_a", "arguments": "{}"},
                    {"id": "call:b", "name": "sleep_b", "arguments": "{}"},
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
    )
    registry = ToolRegistry()
    probe = SyncConcurrencyProbe()
    registry.register(SleepTool("sleep_a", delay=0.2, probe=probe))
    registry.register(SleepTool("sleep_b", delay=0.2, probe=probe))
    _install_registry_with_explicit_test_descriptors(agent, registry)

    asyncio.run(run_agent_task(agent, "run both"))
    sequences = [event.sequence for event in agent.runtime_session.event_log.iter()]

    assert probe.max_active == 2
    assert sequences == sorted(sequences)


def test_tool_gate_allow_and_rollout_reservation_commit_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:a", "name": "sleep_a", "arguments": "{}"},
                    {"id": "call:b", "name": "sleep_b", "arguments": "{}"},
                ]
            },
            {"text": "done"},
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )
    registry = ToolRegistry()
    registry.register(SleepTool("sleep_a", delay=0.01))
    registry.register(SleepTool("sleep_b", delay=0.01))
    _install_registry_with_explicit_test_descriptors(agent, registry)
    original_extend = InMemoryEventLog.extend_with_materialization_state
    observed_batches: list[tuple[AgentEvent, ...]] = []

    def capture_extend(
        self,
        events,
        *,
        expected_account_state_fingerprint,
        resulting_account_state,
        physical_charge_contract,
        expected_last_sequence,
        deadline_monotonic=None,
    ):
        batch = tuple(events)
        if self is runtime_session.event_log:
            observed_batches.append(batch)
        return original_extend(
            self,
            batch,
            expected_account_state_fingerprint=expected_account_state_fingerprint,
            resulting_account_state=resulting_account_state,
            physical_charge_contract=physical_charge_contract,
            expected_last_sequence=expected_last_sequence,
            deadline_monotonic=deadline_monotonic,
        )

    monkeypatch.setattr(
        InMemoryEventLog,
        "extend_with_materialization_state",
        capture_extend,
    )

    result = asyncio.run(run_agent_task(agent, "run both"))

    assert result.status is LoopStatus.FINISHED
    admission_batches = [
        batch
        for batch in observed_batches
        if any(isinstance(event, RolloutBudgetReservationCreatedEvent) for event in batch)
        and any(isinstance(event, CapabilityGateDecisionEvent) for event in batch)
    ]
    assert len(admission_batches) == 1
    admission = tuple(
        event
        for event in admission_batches[0]
        if isinstance(
            event,
            (CapabilityGateDecisionEvent, RolloutBudgetReservationCreatedEvent),
        )
    )
    assert [type(event) for event in admission] == [
        CapabilityGateDecisionEvent,
        RolloutBudgetReservationCreatedEvent,
        CapabilityGateDecisionEvent,
        RolloutBudgetReservationCreatedEvent,
    ]
    physical_reservations = tuple(
        event
        for event in admission_batches[0]
        if isinstance(event, PhysicalOperationReservationCreatedEvent)
    )
    assert len(physical_reservations) == 2
    assert {event.reservation.owner_id for event in physical_reservations} == {
        "call:a",
        "call:b",
    }
    physical_settlements = tuple(
        event
        for event in runtime_session.event_log.iter()
        if isinstance(event, PhysicalOperationReservationSettledEvent)
        and event.settlement.owner_kind.value == "tool_call"
    )
    assert {event.settlement.reservation_id for event in physical_settlements} == {
        event.reservation.reservation_id for event in physical_reservations
    }
    account = runtime_session.event_log.read_materialization_account_state()
    assert account is not None
    assert account.active_reservations == ()


def test_concurrent_tool_admission_failure_leaves_no_partial_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:a", "name": "readonly_a", "arguments": "{}"},
                    {"id": "call:b", "name": "readonly_b", "arguments": "{}"},
                ]
            }
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )
    registry = ToolRegistry()
    registry.register(
        RecordingTool(
            "readonly_a", calls=calls, is_read_only=True, is_concurrency_safe=True
        )
    )
    registry.register(
        RecordingTool(
            "readonly_b", calls=calls, is_read_only=True, is_concurrency_safe=True
        )
    )
    _install_registry_with_explicit_test_descriptors(agent, registry)
    store_type = type(runtime_session.long_horizon_state_store)
    original_validate = store_type.validate_next_batch

    def reject_concurrent_admission(self, events):
        if sum(
            isinstance(event, RolloutBudgetReservationCreatedEvent)
            and event.reservation.owner_kind == "tool_call"
            for event in events
        ) > 1:
            raise RuntimeError("injected tool admission validation failure")
        return original_validate(self, events)

    monkeypatch.setattr(store_type, "validate_next_batch", reject_concurrent_admission)

    with pytest.raises(
        RuntimeError,
        match="injected tool admission validation failure",
    ):
        asyncio.run(run_agent_task(agent, "run both"))
    events = runtime_session.event_log.iter()

    assert calls == []
    assert not any(
        isinstance(event, RolloutBudgetReservationCreatedEvent)
        and event.reservation.owner_kind == "tool_call"
        for event in events
    )
    assert runtime_session.tool_execution_terminal_registry.active_owner_count() == 0


def test_native_async_tools_in_one_model_batch_share_main_loop_and_run_concurrently(
    tmp_path,
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:a", "name": "async_a", "arguments": "{}"},
                    {"id": "call:b", "name": "async_b", "arguments": "{}"},
                ]
            },
            {"text": "done"},
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )
    shared: dict[str, object] = {}
    registry = ToolRegistry()
    registry.register(AsyncConcurrencyProbeTool("async_a", shared))
    registry.register(AsyncConcurrencyProbeTool("async_b", shared))
    _install_registry_with_explicit_test_descriptors(agent, registry)

    result = asyncio.run(run_agent_task(agent, "run both async tools"))

    assert result.status is LoopStatus.FINISHED
    assert shared["max_active"] == 2
    contexts = shared["contexts"]
    assert len(contexts) == 2  # type: ignore[arg-type]
    assert all(
        context.runtime_session_id == runtime_session.runtime_session_id
        and context.event_context.run_id == result.state.run_id
        for context in contexts  # type: ignore[union-attr]
    )


def test_two_memory_search_calls_in_one_model_batch_run_concurrently_with_trace_context(
    tmp_path,
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:memory-a",
                        "name": "memory_search",
                        "arguments": '{"query":"alpha"}',
                    },
                    {
                        "id": "call:memory-b",
                        "name": "memory_search",
                        "arguments": '{"query":"beta"}',
                    },
                ]
            },
            {"text": "done"},
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )
    recall = _ConcurrentRecallService()
    registry = ToolRegistry()
    registry.register(MemorySearchTool(recall=recall))  # type: ignore[arg-type]
    agent.tool_executor.registry = registry

    result = asyncio.run(run_agent_task(agent, "search twice"))

    assert result.status is LoopStatus.FINISHED
    assert recall.max_active == 2
    assert {query.text for query in recall.queries} == {"alpha", "beta"}
    assert all(
        query.session_id == runtime_session.runtime_session_id
        and query.run_id == result.state.run_id
        and query.turn_id is not None
        and query.reply_id is not None
        for query in recall.queries
    )


def test_concurrent_tool_observer_hooks_see_canonical_sequence_order(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:a", "name": "sleep_a", "arguments": "{}"},
                    {"id": "call:b", "name": "sleep_b", "arguments": "{}"},
                ]
            },
            {"text": "done"},
        ]
    )
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )
    registry = ToolRegistry()
    registry.register(SleepTool("sleep_a", delay=0.2))
    registry.register(SleepTool("sleep_b", delay=0.2))
    _install_registry_with_explicit_test_descriptors(agent, registry)
    seen_sequences: list[int] = []

    def record_tool_result_sequences(context, event) -> None:
        if event.type.name.startswith("TOOL_RESULT") and event.sequence is not None:
            seen_sequences.append(event.sequence)

    runtime_session.hook_manager.register_event(None, record_tool_result_sequences)

    result = asyncio.run(run_agent_task(agent, "run both"))

    assert result.status is LoopStatus.FINISHED
    assert seen_sequences == sorted(seen_sequences)
