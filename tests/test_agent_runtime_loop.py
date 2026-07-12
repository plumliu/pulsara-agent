import asyncio
import json
import threading
import time
from dataclasses import dataclass, field, replace
from typing import AsyncIterator

import pytest

from tests.conftest import run_end_contract_fields, run_start_permission_fields
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import (
    AgentEvent,
    ContextCompiledEvent,
    EventContext,
    EventType,
    McpCapabilitySnapshotInstalledEvent,
    ModelCallStartEvent,
    ModelCallRejectedEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
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
from pulsara_agent.llm import LLMRuntime, MessageRole
from pulsara_agent.llm.errors import ModelContextIdentityMismatch
from tests.support import run_agent_task, stream_agent_task, test_llm_config
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.primitives.mcp import (
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpServerLifecycleTimingFact,
)
from pulsara_agent.primitives.capability import CapabilityExecutionSurfaceIdentityFact
from pulsara_agent.memory.recall.service import RecallResult, RecallStatus
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
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
    ThinkingBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultPreviewMetadata,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.runtime import (
    ApprovalResolution,
    AgentRuntime,
    EventPublicationAfterCommitError,
    InRunRecoveryCause,
    LoopBudget,
    LoopState,
    LoopStatus,
    LoopTransition,
    ToolApprovalDecision,
    build_tool_result_error_events,
)
from pulsara_agent.runtime.context import msg_to_llm_messages as _msg_to_llm_messages
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
from pulsara_agent.runtime.terminal import TerminalStatus
from pulsara_agent.runtime.hooks import NoopMemoryHooks
from pulsara_agent.runtime.mcp.types import McpPendingInstallationAudit
from pulsara_agent.runtime.plan import McpInputRequiredInteractionResolution
from pulsara_agent.runtime.tool_artifacts import ToolResultArtifactRecord
from pulsara_agent.runtime.tool_loop import _tool_result_from_event_slice
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


def msg_to_llm_messages(messages, budget, **kwargs):
    kwargs.setdefault("token_estimator", PulsaraHeuristicTokenEstimatorV1())
    return _msg_to_llm_messages(messages, budget, **kwargs)


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
            yield TextBlockStartEvent(
                **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
            )
            yield TextBlockDeltaEvent(
                **event_context.event_fields(),
                block_id=f"text:{len(self.contexts)}",
                delta=reply["text"],
            )
            yield TextBlockEndEvent(
                **event_context.event_fields(), block_id=f"text:{len(self.contexts)}"
            )
        for call in reply.get("tool_calls", []):
            yield ToolCallStartEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield ToolCallDeltaEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                delta=call["arguments"],
            )
            yield ToolCallEndEvent(
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


class RewritingContextCompactor:
    def __init__(self, rewritten_messages: tuple[Msg, ...]) -> None:
        self.rewritten_messages = rewritten_messages
        self.calls = 0

    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
        protected_model_visible_messages_after,
    ):
        assert protected_model_visible_messages_after
        self.calls += 1
        state.scratchpad["mid_turn_compaction"] = {
            "compaction_id": f"fake:{self.calls}"
        }
        return MidTurnCompactionResult(
            compacted=True, rewritten_messages=self.rewritten_messages
        )


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
    original_extend = InMemoryEventLog.extend

    def record_extend(self, events, *, expected_last_sequence=None):
        batch = tuple(events)
        recorded_batches.append(tuple(event.type for event in batch))
        return original_extend(
            self,
            batch,
            expected_last_sequence=expected_last_sequence,
        )

    monkeypatch.setattr(InMemoryEventLog, "extend", record_extend)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
    )

    asyncio.run(run_agent_task(agent, "atomic installation"))

    assert recorded_batches[0] == (
        EventType.RUN_START,
        EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED,
    )
    stored = runtime_session.event_log.iter()
    assert isinstance(stored[0], RunStartEvent)
    assert isinstance(stored[1], McpCapabilitySnapshotInstalledEvent)
    assert stored[0].mcp_installation_id == stored[1].installation_id
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

    def fail_before_commit(self, events, *, expected_last_sequence=None):
        raise RuntimeError("synthetic pre-commit failure")

    monkeypatch.setattr(InMemoryEventLog, "extend", fail_before_commit)
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
    assert len(
        [
            event
            for event in runtime_session.event_log.iter()
            if isinstance(event, McpCapabilitySnapshotInstalledEvent)
        ]
    ) == 1

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

    assert [result.id for result in state.tool_results] == [
        "call:mcp-post-commit"
    ]
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

        def abort_pending_lease(
            self, interaction_id: str, reservation_id: str
        ) -> None:
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

    async def commit_then_wait(self, event, *, state=None):
        assert self is runtime_session
        del state
        prepared = runtime_session._prepare_event_batch((event,))
        stored = runtime_session.event_log.append(prepared[0])
        committed.set()
        await asyncio.Event().wait()
        return stored

    monkeypatch.setattr(type(runtime_session), "emit", commit_then_wait)
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
        async def consume() -> None:
            async for _ in agent._suspend_tool_execution(state, suspended):
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
        if getattr(event, "name", None) == "tool_execution_suspended"
    ]
    assert len(suspension_events) == 1
    assert supervisor.confirmed == [
        (
            "mcp_input_required:suspend-cancel",
            "reservation:suspend-cancel",
        )
    ]
    assert supervisor.aborted == []
    assert state.status is LoopStatus.WAITING_USER
    assert state.pending_interaction_kind == "mcp_input_required"


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
            raise RuntimeError("synthetic precommit failure")

        monkeypatch.setattr(type(runtime_session), "emit_many", fail_before_commit)
    elif failure_mode == "cancel_after_commit":
        async def commit_then_wait(self, events, *, state=None):
            assert self is runtime_session
            del state
            prepared = runtime_session._prepare_event_batch(tuple(events))
            stored = runtime_session.event_log.extend(prepared)
            committed.set()
            await asyncio.Event().wait()
            return stored

        monkeypatch.setattr(type(runtime_session), "emit_many", commit_then_wait)
    else:
        async def fail_before_unknown_confirmation(self, _events, *, state=None):
            assert self is runtime_session
            del state
            raise RuntimeError("synthetic unknown commit acknowledgement")

        def fail_confirmation(self, _events):
            assert self is runtime_session
            raise RuntimeError("synthetic confirmation read failure")

        monkeypatch.setattr(
            type(runtime_session),
            "emit_many",
            fail_before_unknown_confirmation,
        )
        monkeypatch.setattr(
            type(runtime_session), "confirm_event_batch", fail_confirmation
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
        if isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == tool_call_id
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

    def reject_stream(**kwargs):
        async def generate():
            raise ModelContextIdentityMismatch("synthetic compiled identity rejection")
            yield  # pragma: no cover

        return generate()

    agent.llm_runtime.stream = reject_stream
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


def test_agent_runtime_retries_after_recoverable_context_pressure_compaction(
    tmp_path,
) -> None:
    transport = ScriptedTransport([{"text": "after retry"}])
    runtime_session = in_memory_runtime_session(tmp_path)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(transport),
        budget=LoopBudget(
            tool_result_context_chars=40,
            current_tail_tool_result_context_chars=0,
            latest_tool_result_reserved_chars=0,
        ),
    )
    state = LoopState(session_id=runtime_session.runtime_session_id)
    state.run_model_target = agent.resolve_run_model_target()
    run_start_fields = run_start_permission_fields(
        state.run_id,
        user_input="continue after pressure",
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        model_target=state.run_model_target.fact,
    )
    state.scratchpad["terminal_run_end_event_id"] = run_start_fields[
        "terminal_run_end_event_id"
    ]
    user = UserMsg(
        name="user",
        content="continue after pressure",
        id=f"user-message:{state.run_id}",
        metadata={"run_id": state.run_id},
    )
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call:terminal", name="terminal", input='{"cmd":"x"}')
        ],
    )
    pressure_result = AssistantMsg(
        name="assistant",
        content=[
            ToolResultBlock(
                id="call:terminal",
                name="terminal",
                output=[
                    TextBlock(
                        text=json.dumps(
                            {
                                "status": "success",
                                "output": "body omitted",
                                "exit_code": 0,
                                "cwd": "/workspace",
                                "process_id": "proc:1",
                                "terminal_session_id": "default",
                                "backend_type": "local",
                            }
                        )
                    )
                ],
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    state.messages.extend([user, assistant, pressure_result])
    compactor = RewritingContextCompactor((user,))
    agent.context_compactor = compactor
    exposure = CapabilityExposurePlan(
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

    async def run_loop() -> list[AgentEvent]:
        await runtime_session.emit(
            RunStartEvent(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                **run_start_fields,
                user_input_chars=len("continue after pressure"),
            ),
            state=state,
        )
        return await _collect_async(agent._stream_model_loop(state, exposure))

    events = asyncio.run(run_loop())

    compiled_events = [
        event for event in events if isinstance(event, ContextCompiledEvent)
    ]
    assert [event.status for event in compiled_events] == ["pressure", "compiled"]
    assert (
        compiled_events[0].model_call_index == compiled_events[1].model_call_index == 1
    )
    assert compiled_events[0].compile_attempt_index == 1
    assert compiled_events[1].compile_attempt_index == 2
    assert compiled_events[1].context_retry_index == 1
    assert (
        compiled_events[0].resolved_call.resolved_model_call_id
        == compiled_events[1].resolved_call.resolved_model_call_id
    )
    assert compiled_events[0].context_id != compiled_events[1].context_id
    assert compactor.calls == 1
    assert len(transport.contexts) == 1
    assert transport.contexts[0].model_call_index == 1


def test_compile_retry_reuses_call_id(tmp_path) -> None:
    test_agent_runtime_retries_after_recoverable_context_pressure_compaction(tmp_path)


def test_compile_retry_may_change_context_id(tmp_path) -> None:
    test_agent_runtime_retries_after_recoverable_context_pressure_compaction(tmp_path)


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


def test_tool_followup_uses_new_call_id(tmp_path) -> None:
    test_run_followups_reuse_target(tmp_path)


def test_msg_to_llm_messages_compresses_context_blocks() -> None:
    huge = "x" * 40
    messages = [
        UserMsg(name="user", content="hello"),
        AssistantMsg(
            name="assistant",
            content=[
                TextBlock(text="visible"),
                ThinkingBlock(thinking="hidden"),
                ToolCallBlock(id="call:ignored", name="lookup", input="{}"),
                ToolResultBlock(
                    id="call:1",
                    name="terminal",
                    output=[TextBlock(text=huge)],
                    state=ToolResultState.SUCCESS,
                ),
                DataBlock(
                    id="data:plot",
                    source=Base64Source(data="abc", media_type="image/png"),
                    name="plot",
                ),
            ],
        ),
    ]

    llm_messages = msg_to_llm_messages(
        messages, LoopBudget(tool_result_context_chars=20)
    )
    assistant_text = "\n".join(
        text
        for message in llm_messages
        if message.role is MessageRole.ASSISTANT
        for text in message.content
    )
    assistant_turn = next(
        message
        for message in llm_messages
        if message.role is MessageRole.ASSISTANT and message.tool_calls
    )
    tool_call = assistant_turn.tool_calls[0]
    tool_result = next(
        message for message in llm_messages if message.role is MessageRole.TOOL_RESULT
    )

    assert "visible" in assistant_text
    assert "hidden" not in assistant_text
    assert "call:ignored" not in assistant_text
    assert assistant_turn.thinking == ("hidden",)
    assert tool_call.id == "call:ignored"
    assert tool_call.name == "lookup"
    assert "TOOL RESULT BODY OMITTED" in "\n".join(tool_result.content)
    assert tool_result.tool_call_id == "call:1"
    assert "data block omitted" in assistant_text
    assert "abc" not in assistant_text


def test_msg_to_llm_messages_wraps_artifact_tool_results_after_clipping() -> None:
    messages = [
        Msg(
            role="tool_result",
            name="terminal",
            content=[
                ToolResultBlock(
                    id="call:artifact",
                    name="terminal",
                    output=[
                        TextBlock(
                            text='{"status":"success","output":"' + ("x" * 80) + '"}'
                        )
                    ],
                    state=ToolResultState.SUCCESS,
                    artifacts=[
                        ToolResultArtifactRef(
                            artifact_id="artifact:tool-result:run:call:combined_output:0",
                            role="combined_output",
                            media_type="text/plain; charset=utf-8",
                            size_bytes=200,
                        )
                    ],
                )
            ],
        )
    ]

    llm_messages = msg_to_llm_messages(
        messages,
        LoopBudget(
            tool_result_context_chars=1_000,
            tool_result_body_context_chars=0,
            legacy_tool_result_context_chars=0,
        ),
    )
    content = "\n".join(llm_messages[0].content)
    body = content.split("\n", 1)[1]
    envelope = json.loads(body)

    assert envelope["output_truncated"] is True
    assert "omitted" in envelope["output_preview"].lower()
    assert (
        envelope["artifacts"][0]["artifact_id"]
        == "artifact:tool-result:run:call:combined_output:0"
    )
    assert (
        envelope["artifacts"][0]["read_more"]["artifact_id"]
        == "artifact:tool-result:run:call:combined_output:0"
    )


def test_msg_to_llm_messages_uses_aggregate_tool_result_budget() -> None:
    messages = [
        Msg(
            role="tool_result",
            name="first",
            content=[
                ToolResultBlock(
                    id="call:first",
                    name="first",
                    output=[TextBlock(text="A" * 18)],
                    state=ToolResultState.SUCCESS,
                )
            ],
        ),
        Msg(
            role="tool_result",
            name="second",
            content=[
                ToolResultBlock(
                    id="call:second",
                    name="second",
                    output=[TextBlock(text="B" * 18)],
                    state=ToolResultState.SUCCESS,
                )
            ],
        ),
    ]

    llm_messages = msg_to_llm_messages(
        messages, LoopBudget(tool_result_context_chars=90)
    )
    first = "\n".join(llm_messages[0].content)
    second = "\n".join(llm_messages[1].content)

    assert "A" * 18 in first
    assert "TOOL RESULT BODY OMITTED" in second


def test_msg_to_llm_messages_bounds_artifact_envelopes_after_budget_exhaustion() -> (
    None
):
    noisy_preview = ToolResultPreviewMetadata(
        preview_policy="head_tail",
        preview_chars=8_000,
        original_chars=80_000,
        original_bytes=80_000,
        omitted_middle_chars=72_000,
        visible_head_chars=5_000,
        visible_tail_chars=3_000,
        read_more={
            "tool": "artifact_read",
            "artifact_id": "artifact:huge",
            "suggested_offset_chars": 5_000,
            "suggested_max_chars": 20_000,
            "noise": "N" * 5_000,
        },
    )
    messages = [
        Msg(
            role="tool_result",
            name=f"terminal-{idx}",
            content=[
                ToolResultBlock(
                    id=f"call:{idx}",
                    name="terminal",
                    output=[TextBlock(text="x" * 100)],
                    state=ToolResultState.SUCCESS,
                    artifacts=[
                        ToolResultArtifactRef(
                            artifact_id=f"artifact:{idx}",
                            role="combined_output",
                            media_type="text/plain; charset=utf-8",
                            size_bytes=80_000,
                            preview=noisy_preview,
                        )
                    ],
                )
            ],
        )
        for idx in range(5)
    ]

    llm_messages = msg_to_llm_messages(
        messages, LoopBudget(tool_result_context_chars=500)
    )
    rendered = "\n".join("\n".join(message.content) for message in llm_messages)

    assert "TOOL RESULT BODY OMITTED" in rendered
    assert len(rendered) < 4_000
    assert '"noise"' not in rendered


def test_msg_to_llm_messages_preserves_terminal_essential_envelope_when_body_is_omitted() -> (
    None
):
    payload = {
        "status": "success",
        "output": "VISIBLE_ONLY_IF_BUDGET_AVAILABLE",
        "exit_code": 0,
        "cwd": "/workspace",
        "timed_out": False,
        "truncated": False,
        "error": None,
        "process_id": "proc:123",
        "yielded_to_background": True,
        "terminal_session_id": "default",
        "backend_type": "local",
        "io_mode": "pipe",
    }
    messages = [
        Msg(
            role="tool_result",
            name="terminal",
            content=[
                ToolResultBlock(
                    id="call:terminal",
                    name="terminal",
                    output=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
    ]

    llm_messages = msg_to_llm_messages(
        messages,
        LoopBudget(tool_result_context_chars=700, tool_result_body_context_chars=0),
    )
    rendered = "\n".join(llm_messages[0].content)
    envelope = json.loads(rendered.split("\n", 1)[1])

    assert envelope["tool_result_body_omitted"] is True
    assert envelope["status"] == "success"
    assert envelope["exit_code"] == 0
    assert envelope["cwd"] == "/workspace"
    assert envelope["process_id"] == "proc:123"
    assert envelope["yielded_to_background"] is True
    assert envelope["terminal_session_id"] == "default"
    assert envelope["backend_type"] == "local"
    assert "VISIBLE_ONLY_IF_BUDGET_AVAILABLE" not in rendered


def test_msg_to_llm_messages_preserves_terminal_essential_envelope_when_json_is_clipped() -> (
    None
):
    payload = {
        "status": "success",
        "output": "x" * 1_000,
        "exit_code": 0,
        "cwd": "/workspace",
        "process_id": "proc:small-budget",
        "terminal_session_id": "default",
        "backend_type": "local",
    }
    messages = [
        Msg(
            role="tool_result",
            name="terminal",
            content=[
                ToolResultBlock(
                    id="call:terminal-small-budget",
                    name="terminal",
                    output=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
    ]

    llm_messages = msg_to_llm_messages(
        messages,
        LoopBudget(tool_result_context_chars=700, tool_result_body_context_chars=120),
    )
    rendered = "\n".join(llm_messages[0].content)
    envelope = json.loads(rendered.split("\n", 1)[1])

    assert envelope["tool_result_body_omitted"] is True
    assert envelope["status"] == "success"
    assert envelope["exit_code"] == 0
    assert envelope["cwd"] == "/workspace"
    assert envelope["process_id"] == "proc:small-budget"
    assert "TOOL RESULT BODY TRUNCATED" not in rendered
    assert "x" * 80 not in rendered


def test_msg_to_llm_messages_does_not_use_terminal_envelope_for_read_file_json() -> (
    None
):
    payload = {
        "status": "ok",
        "path": "large.txt",
        "access_scope": "workspace",
        "workspace_relative": True,
        "offset": 1,
        "limit": 200,
        "total_lines": 400,
        "file_size": 20_000,
        "truncated": True,
        "content": "LINE\n" * 1_000,
    }
    messages = [
        Msg(
            role="tool_result",
            name="read_file",
            content=[
                ToolResultBlock(
                    id="call:read-file-json",
                    name="read_file",
                    output=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
    ]

    llm_messages = msg_to_llm_messages(
        messages, LoopBudget(tool_result_context_chars=500)
    )
    rendered = "\n".join(llm_messages[0].content)

    assert "tool_result_body_omitted" not in rendered
    assert "TOOL RESULT BODY TRUNCATED" in rendered
    assert rendered.split("\n", 1)[1].startswith('{"status": "ok"')


def test_msg_to_llm_messages_does_not_use_terminal_envelope_for_custom_exec_json() -> (
    None
):
    payload = {
        "status": "ok",
        "exit_code": 0,
        "cwd": "/remote/project",
        "output": "BUSINESS_EXECUTION_SUMMARY\n" * 200,
    }
    messages = [
        Msg(
            role="tool_result",
            name="custom_mcp_exec_summary",
            content=[
                ToolResultBlock(
                    id="call:custom-exec-json",
                    name="custom_mcp_exec_summary",
                    output=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
    ]

    llm_messages = msg_to_llm_messages(
        messages, LoopBudget(tool_result_context_chars=500)
    )
    rendered = "\n".join(llm_messages[0].content)

    assert "tool_result_body_omitted" not in rendered
    assert "TOOL RESULT BODY TRUNCATED" in rendered
    assert rendered.split("\n", 1)[1].startswith('{"status": "ok"')


def test_msg_to_llm_messages_preserves_terminal_process_list_summary_when_body_is_omitted() -> (
    None
):
    payload = {
        "status": "success",
        "terminal_process_action": "list",
        "processes": [
            {
                "process_id": "proc:running",
                "status": "running",
                "cwd": "/workspace",
                "exit_code": None,
            },
            {
                "process_id": "proc:old-done",
                "status": "success",
                "cwd": "/workspace/old",
                "exit_code": 0,
                "ended_at_monotonic": 10.0,
            },
            {
                "process_id": "proc:recent-done",
                "status": "success",
                "cwd": "/workspace/recent",
                "exit_code": 0,
                "ended_at_monotonic": 20.0,
            },
        ],
        "live_process_count": 1,
        "finished_process_count": 2,
    }
    messages = [
        Msg(
            role="tool_result",
            name="terminal_process",
            content=[
                ToolResultBlock(
                    id="call:terminal-process-list",
                    name="terminal_process",
                    output=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
    ]

    llm_messages = msg_to_llm_messages(
        messages,
        LoopBudget(tool_result_context_chars=1_000, tool_result_body_context_chars=0),
    )
    rendered = "\n".join(llm_messages[0].content)
    envelope = json.loads(rendered.split("\n", 1)[1])

    assert envelope["tool_result_body_omitted"] is True
    assert envelope["terminal_process_action"] == "list"
    assert envelope["live_process_count"] == 1
    assert envelope["finished_process_count"] == 2
    assert envelope["processes_summary"][0]["process_id"] == "proc:running"
    assert envelope["processes_summary"][1]["process_id"] == "proc:recent-done"
    assert envelope["processes_summary"][2]["process_id"] == "proc:old-done"


def test_msg_to_llm_messages_compact_envelope_keeps_primary_preview_artifact() -> None:
    preview = ToolResultPreviewMetadata(
        preview_policy="head_tail",
        preview_chars=8_000,
        original_chars=80_000,
        original_bytes=80_000,
        omitted_middle_chars=72_000,
        visible_head_chars=5_000,
        visible_tail_chars=3_000,
        read_more={
            "tool": "artifact_read",
            "artifact_id": "artifact:combined",
            "suggested_offset_chars": 5_000,
            "suggested_max_chars": 20_000,
        },
    )
    messages = [
        Msg(
            role="tool_result",
            name="terminal",
            content=[
                ToolResultBlock(
                    id="call:terminal",
                    name="terminal",
                    output=[TextBlock(text="x" * 100)],
                    state=ToolResultState.SUCCESS,
                    artifacts=[
                        ToolResultArtifactRef(
                            artifact_id="artifact:diagnostics",
                            role="diagnostics",
                            media_type="application/json",
                            size_bytes=512,
                        ),
                        ToolResultArtifactRef(
                            artifact_id="artifact:combined",
                            role="combined_output",
                            media_type="text/plain; charset=utf-8",
                            size_bytes=80_000,
                            preview=preview,
                        ),
                    ],
                )
            ],
        )
    ]

    llm_messages = msg_to_llm_messages(
        messages, LoopBudget(tool_result_context_chars=1)
    )
    rendered = "\n".join("\n".join(message.content) for message in llm_messages)
    envelope = json.loads(rendered.split("\n", 1)[1])

    assert envelope["artifacts"][0]["artifact_id"] == "artifact:combined"
    assert (
        envelope["artifacts"][0]["preview"]["read_more"]["artifact_id"]
        == "artifact:combined"
    )
    assert envelope["artifact_refs_omitted"] == 1
    assert "artifact:diagnostics" not in rendered


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
        event.type is EventType.TEXT_BLOCK_DELTA
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


def test_agent_runtime_accepts_prior_messages(tmp_path) -> None:
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
    assert "previous sentinel" in context_text
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
    assert EventType.TEXT_BLOCK_DELTA in seen_events
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
        EventType.TOOL_RESULT_END,
    ]
    assert any("hook file" in text for text in seen_tool_result_blocks)


def test_agent_runtime_hook_error_does_not_break_run(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    def failing_hook(context, event) -> None:
        if event.type is EventType.TEXT_BLOCK_DELTA:
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


def test_agent_runtime_exceeds_max_turns(tmp_path) -> None:
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
            }
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        budget=LoopBudget(max_turns=1),
    )

    result = asyncio.run(run_agent_task(agent, "Read forever."))

    assert result.status is LoopStatus.FAILED
    assert result.stop_reason == "max_turns"
    assert any(
        event.type is EventType.EXCEED_MAX_ITERS
        for event in agent.runtime_session.event_log.iter()
    )


def test_build_tool_result_error_events_use_standard_event_shape() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    event_log = InMemoryEventLog()
    context = EventContext(
        run_id="run:test", turn_id="turn:test", reply_id="reply:test"
    )

    events = event_log.extend(
        build_tool_result_error_events(
            context,
            tool_call_id="call:bad",
            tool_call_name="lookup",
            message="bad json",
        )
    )

    assert [event.type for event in events] == [
        EventType.TOOL_RESULT_START,
        EventType.TOOL_RESULT_TEXT_DELTA,
        EventType.TOOL_RESULT_END,
    ]
    assert event_log.replay("reply:test").content[0].state is ToolResultState.ERROR


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
    context_text = "\n".join(
        text for message in transport.contexts[0].messages for text in message.content
    )
    assert "Recalled Memory" in context_text


def test_run_end_known_precommit_failure_retries_stable_candidate_once(
    tmp_path,
) -> None:
    class FailOnceRunEndLog(InMemoryEventLog):
        def __init__(self) -> None:
            super().__init__()
            self.run_end_attempts = 0

        def extend(self, events, *, expected_last_sequence=None):
            batch = tuple(events)
            if any(isinstance(event, RunEndEvent) for event in batch):
                self.run_end_attempts += 1
                if self.run_end_attempts == 1:
                    raise RuntimeError("synthetic precommit RunEnd failure")
            return super().extend(
                batch,
                expected_last_sequence=expected_last_sequence,
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
        entry.capability_name
        for entry in provider.execution_surfaces[0].entries
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
            TextBlockDeltaEvent(
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
    block = runtime_session.event_log.replay("reply:read").content[0]
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
class SleepTool:
    name: str
    delay: float
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    description: str = "Sleep briefly."
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        time.sleep(self.delay)
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
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=call.id,
        )


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
    state.permission_snapshot = agent._capture_run_permission_snapshot(state)
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
    state.scratchpad["capability_execution_borrow_authority"] = (
        handles.borrow_authority
    )

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


def test_duplicate_tool_call_id_becomes_error_observation_without_execution(
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
    second_context_text = "\n".join(
        text for msg in transport.contexts[1].messages for text in msg.content
    )

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "recovered"
    assert calls == []
    assert "Duplicate tool_call_id in assistant reply: call:dup" in second_context_text
    assert any(
        isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:dup"
        and event.state is ToolResultState.ERROR
        for event in agent.runtime_session.event_log.iter()
    )


def test_duplicate_tool_call_id_only_blocks_the_duplicate_calls(tmp_path) -> None:
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
    second_context_text = "\n".join(
        text for msg in transport.contexts[1].messages for text in msg.content
    )

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "recovered"
    assert calls == ["call:ok"]
    assert "Duplicate tool_call_id in assistant reply: call:dup" in second_context_text
    assert "call:ok" in second_context_text
    assert any(
        isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:dup"
        and event.state is ToolResultState.ERROR
        for event in agent.runtime_session.event_log.iter()
    )
    assert any(
        isinstance(event, ToolResultEndEvent)
        and event.tool_call_id == "call:ok"
        and event.state is ToolResultState.SUCCESS
        for event in agent.runtime_session.event_log.iter()
    )


def test_tool_budget_blocks_unsafe_tool_before_execution(tmp_path) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:write", "name": "write_side_effect", "arguments": "{}"}
                ]
            },
            {"text": "should not run"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        budget=LoopBudget(max_tool_calls=0),
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("write_side_effect", calls=calls))
    _install_registry_with_explicit_test_descriptors(agent, registry)

    result = asyncio.run(run_agent_task(agent, "run unsafe tool"))
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)

    assert result.status is LoopStatus.FAILED
    assert result.stop_reason == "tool_error_budget"
    assert calls == []
    assert not any(isinstance(event, ToolResultStartEvent) for event in events)
    error = next(event for event in events if isinstance(event, RunErrorEvent))
    assert error.code == "tool_budget_exceeded"
    assert error.metadata["attempted_tool_call_count"] == 1
    assert len(transport.contexts) == 1


def test_tool_budget_blocks_concurrent_batch_before_partial_execution(tmp_path) -> None:
    calls: list[str] = []
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:a", "name": "readonly_a", "arguments": "{}"},
                    {"id": "call:b", "name": "readonly_b", "arguments": "{}"},
                ]
            },
            {"text": "should not run"},
        ]
    )
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        budget=LoopBudget(max_tool_calls=1),
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

    result = asyncio.run(run_agent_task(agent, "run readonly tools"))
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)

    assert result.status is LoopStatus.FAILED
    assert result.stop_reason == "tool_error_budget"
    assert calls == []
    assert not any(isinstance(event, ToolResultStartEvent) for event in events)
    error = next(event for event in events if isinstance(event, RunErrorEvent))
    assert error.code == "tool_budget_exceeded"
    assert error.metadata["attempted_tool_call_count"] == 2
    assert len(transport.contexts) == 1


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
    registry.register(SleepTool("sleep_a", delay=0.2))
    registry.register(SleepTool("sleep_b", delay=0.2))
    _install_registry_with_explicit_test_descriptors(agent, registry)

    started = time.monotonic()
    asyncio.run(run_agent_task(agent, "run both"))
    elapsed = time.monotonic() - started
    sequences = [event.sequence for event in agent.runtime_session.event_log.iter()]

    assert elapsed < 0.35
    assert sequences == sorted(sequences)


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
