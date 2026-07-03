"""Tests for the durable-memory producer: sink, DurableMemoryHooks, remember_*.

The producer bridges the agent loop to the durable-memory candidate pool. A
tool deposits a candidate-pool proposal into a sink during tool execution; a
hook drains the sink at an agent-loop-safe point and appends the envelope to
the durable pool. Canonical memory writes are owned by governance.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    EventType,
    ModelCallEndEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.event.candidates import (
    ActionBoundaryCandidate,
    DecisionCandidate,
    InvalidAttemptPayload,
    PreferenceCandidate,
    ValidCandidatePayload,
)
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.hooks.durable import DurableMemoryHooks
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    CandidatePoolProposal,
    InMemoryCandidatePool,
)
from pulsara_agent.memory.governance.executor import MemoryGovernanceExecutor
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.runtime import AgentRuntime, LoopState
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.tools.base import ToolCall
from pulsara_agent.tools.builtins.memory import (
    RememberActionBoundaryTool,
    RememberDecisionTool,
    RememberPreferenceTool,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ontology import memory
from tests.support.memory_uow import fake_memory_uow_factory
from tests.support.runtime_session import in_memory_runtime_session


CTX = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")


def _service_on(graph: InMemoryGraphStore) -> MemoryWriteService:
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    return MemoryWriteService(ledger=ledger)


def _preference(candidate_id: str = "candidate:pref") -> PreferenceCandidate:
    return PreferenceCandidate(
        candidate_id=candidate_id,
        statement="Prefer concise summaries.",
        scope="ctx:user",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )


# --- MemoryProposalSink ---------------------------------------------------


def test_sink_drain_returns_and_clears() -> None:
    sink = MemoryProposalSink()
    sink.deposit(_proposal(_preference("candidate:a")))
    sink.deposit(_proposal(_preference("candidate:b")))

    assert sink.pending_count() == 2
    drained = sink.drain()
    assert [
        proposal.payload.candidate.candidate_id
        for proposal in drained
        if isinstance(proposal.payload, ValidCandidatePayload)
    ] == ["candidate:a", "candidate:b"]
    assert sink.pending_count() == 0
    assert sink.drain() == []


def test_sink_stages_invalid_attempts_until_finalization() -> None:
    sink = MemoryProposalSink()

    first = sink.record_invalid(_invalid_proposal("call:1", statement="x"), "intent:one")
    second = sink.record_invalid(_invalid_proposal("call:2", statement="x revised"), "intent:one")
    third = sink.record_invalid(_invalid_proposal("call:3", statement="x final"), "intent:one")

    assert first.retry_allowed is True
    assert second.retry_allowed is True
    assert third.retry_allowed is False
    assert third.retry_count == 3
    assert third.remaining_retries == 0
    assert sink.pending_count() == 1
    assert sink.drain_valid() == []
    finalized = sink.finalize_invalid_attempts()
    assert len(finalized) == 1
    assert finalized[0].source_tool_call_id == "call:3"
    assert isinstance(finalized[0].payload, InvalidAttemptPayload)
    assert finalized[0].payload.raw_arguments["statement"] == "x final"
    assert sink.pending_count() == 0


def test_sink_valid_candidate_clears_staged_invalid_for_same_intent() -> None:
    sink = MemoryProposalSink()
    sink.record_invalid(_invalid_proposal("call:bad", statement="Prefer concise summaries."), "intent:pref")

    sink.deposit_valid(_proposal(_preference("candidate:good")), "intent:pref")

    assert sink.pending_count() == 1
    drained = sink.drain_valid()
    assert len(drained) == 1
    assert isinstance(drained[0].payload, ValidCandidatePayload)
    assert drained[0].payload.candidate.candidate_id == "candidate:good"
    assert sink.finalize_invalid_attempts() == []


def test_sink_counts_different_invalid_intents_independently() -> None:
    sink = MemoryProposalSink()

    first_a = sink.record_invalid(_invalid_proposal("call:a1", statement="a"), "intent:a")
    first_b = sink.record_invalid(_invalid_proposal("call:b1", statement="b"), "intent:b")
    second_a = sink.record_invalid(_invalid_proposal("call:a2", statement="a again"), "intent:a")

    assert first_a.retry_count == 1
    assert first_b.retry_count == 1
    assert second_a.retry_count == 2
    assert first_b.remaining_retries == 2
    finalized = sink.finalize_invalid_attempts()
    assert {proposal.source_tool_call_id for proposal in finalized} == {"call:a2", "call:b1"}


# --- DurableMemoryHooks ---------------------------------------------------


def test_durable_hooks_drain_appends_candidates_without_writing_graph() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    sink = MemoryProposalSink()
    sink.deposit(_proposal(_preference()))
    hooks = DurableMemoryHooks(candidate_pool=pool, sink=sink)
    state = LoopState(session_id="runtime:test")

    events = asyncio.run(hooks.after_tool_results(state, []))

    assert events == []
    pending = pool.list_pending()
    assert len(pending) == 1
    assert isinstance(pending[0].payload, ValidCandidatePayload)
    assert pending[0].payload.candidate.candidate_id == "candidate:pref"
    assert graph.find_by_type(memory.PREFERENCE) == []
    # Drained: a second drain at session end produces nothing.
    assert asyncio.run(hooks.on_session_end(state)) == []


def test_durable_hooks_delays_invalid_attempts_until_session_end() -> None:
    pool = InMemoryCandidatePool()
    sink = MemoryProposalSink()
    sink.record_invalid(_invalid_proposal("call:1", statement="bad"), "intent:bad")
    sink.record_invalid(_invalid_proposal("call:2", statement="last bad"), "intent:bad")
    hooks = DurableMemoryHooks(candidate_pool=pool, sink=sink)
    state = LoopState(session_id="runtime:test")

    assert asyncio.run(hooks.after_tool_results(state, [])) == []
    assert pool.list_pending() == []
    assert sink.pending_count() == 1

    assert asyncio.run(hooks.on_session_end(state)) == []
    pending = pool.list_pending()
    assert len(pending) == 1
    assert isinstance(pending[0].payload, InvalidAttemptPayload)
    assert pending[0].source_tool_call_id == "call:2"
    assert pending[0].payload.raw_arguments["statement"] == "last bad"
    assert sink.pending_count() == 0


def test_durable_hooks_empty_sink_returns_no_events() -> None:
    hooks = DurableMemoryHooks(candidate_pool=InMemoryCandidatePool(), sink=MemoryProposalSink())
    state = LoopState(session_id="runtime:test")

    assert asyncio.run(hooks.after_model_reply(state, None)) == []
    assert asyncio.run(hooks.after_tool_results(state, [])) == []
    assert asyncio.run(hooks.on_session_end(state)) == []


def test_durable_hooks_event_context_comes_from_loop_state() -> None:
    pool = InMemoryCandidatePool()
    sink = MemoryProposalSink()
    sink.deposit(_proposal(_preference()))
    hooks = DurableMemoryHooks(candidate_pool=pool, sink=sink)
    state = LoopState(session_id="runtime:test")

    asyncio.run(hooks.after_tool_results(state, []))

    candidate = pool.list_pending()[0]
    assert candidate.source_run_id == state.run_id
    assert candidate.source_turn_id == state.turn_id
    assert candidate.source_reply_id == state.reply_id
    assert candidate.source_session_id == state.session_id


# --- Remember memory tools ------------------------------------------------


def test_remember_preference_tool_valid_deposits_candidate() -> None:
    sink = MemoryProposalSink()
    tool = RememberPreferenceTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:1",
            name="remember_preference",
            arguments={
                "statement": "Prefer concise summaries.",
                "scope": "ctx:user",
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.SUCCESS
    assert sink.pending_count() == 1
    proposal = sink.drain()[0]
    assert proposal.origin is CandidateOrigin.MAIN_AGENT_TOOL
    assert proposal.source_tool_call_id == "call:1"
    assert isinstance(proposal.payload, ValidCandidatePayload)
    deposited = proposal.payload.candidate
    assert isinstance(deposited, PreferenceCandidate)
    assert deposited.candidate_id.startswith("candidate:")
    assert deposited.kind == "Preference"
    assert json.loads(result.output)["status"] == "proposed"


def test_remember_preference_tool_extra_field_errors_and_stages_invalid_attempt() -> None:
    sink = MemoryProposalSink()
    tool = RememberPreferenceTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:1",
            name="remember_preference",
            arguments={
                "statement": "x",
                "scope": "ctx:user",
                "applies_when": "misplaced action-boundary field",
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.ERROR
    output = json.loads(result.output)
    assert output["status"] == "invalid_candidate"
    assert output["retry_allowed"] is True
    assert output["retry_count"] == 1
    assert output["retry_limit"] == 3
    assert output["remaining_retries"] == 2
    assert sink.pending_count() == 1
    assert sink.drain_valid() == []
    proposal = sink.finalize_invalid_attempts()[0]
    assert isinstance(proposal.payload, InvalidAttemptPayload)
    assert proposal.payload.attempted_tool_name == "remember_preference"
    assert proposal.payload.attempted_kind == "Preference"
    assert proposal.payload.raw_arguments["applies_when"] == "misplaced action-boundary field"


def test_remember_preference_tool_invalid_retry_limit_returns_do_not_retry() -> None:
    sink = MemoryProposalSink()
    tool = RememberPreferenceTool(sink=sink)
    arguments = {
        "statement": "x",
        "scope": "ctx:user",
        "applies_when": "misplaced action-boundary field",
        "source_authority": "explicit_user_instruction",
        "verification_status": "user_confirmed",
    }

    outputs = [
        json.loads(
            tool.execute(
                ToolCall(
                    id=f"call:{index}",
                    name="remember_preference",
                    arguments=arguments,
                )
            ).output
        )
        for index in range(1, 4)
    ]

    assert [output["retry_allowed"] for output in outputs] == [True, True, False]
    assert outputs[-1]["retry_count"] == 3
    assert outputs[-1]["remaining_retries"] == 0
    assert "Do not retry this memory tool" in outputs[-1]["message"]
    finalized = sink.finalize_invalid_attempts()
    assert len(finalized) == 1
    assert finalized[0].source_tool_call_id == "call:3"


def test_remember_preference_tool_valid_after_invalid_clears_staged_invalid() -> None:
    sink = MemoryProposalSink()
    tool = RememberPreferenceTool(sink=sink)

    invalid = tool.execute(
        ToolCall(
            id="call:bad",
            name="remember_preference",
            arguments={
                "statement": "Prefer concise summaries.",
                "scope": "ctx:user",
                "applies_when": "misplaced action-boundary field",
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )
    valid = tool.execute(
        ToolCall(
            id="call:good",
            name="remember_preference",
            arguments={
                "statement": "Prefer concise summaries.",
                "scope": "ctx:user",
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert invalid.status is ToolResultState.ERROR
    assert valid.status is ToolResultState.SUCCESS
    drained = sink.drain_valid()
    assert len(drained) == 1
    assert isinstance(drained[0].payload, ValidCandidatePayload)
    assert drained[0].source_tool_call_id == "call:good"
    assert sink.finalize_invalid_attempts() == []


def test_remember_preference_tool_valid_after_retry_limit_still_clears_invalid() -> None:
    sink = MemoryProposalSink()
    tool = RememberPreferenceTool(sink=sink)
    invalid_arguments = {
        "statement": "Prefer concise summaries.",
        "scope": "ctx:user",
        "applies_when": "misplaced action-boundary field",
        "source_authority": "explicit_user_instruction",
        "verification_status": "user_confirmed",
    }

    for index in range(1, 4):
        result = tool.execute(
            ToolCall(
                id=f"call:bad:{index}",
                name="remember_preference",
                arguments=invalid_arguments,
            )
        )
    assert json.loads(result.output)["retry_allowed"] is False

    valid = tool.execute(
        ToolCall(
            id="call:good",
            name="remember_preference",
            arguments={
                "statement": "Prefer concise summaries.",
                "scope": "ctx:user",
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert valid.status is ToolResultState.SUCCESS
    drained = sink.drain_valid()
    assert len(drained) == 1
    assert drained[0].source_tool_call_id == "call:good"
    assert sink.finalize_invalid_attempts() == []


def test_remember_action_boundary_tool_missing_condition_errors() -> None:
    sink = MemoryProposalSink()
    tool = RememberActionBoundaryTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:1",
            name="remember_action_boundary",
            arguments={
                "statement": "Never force-push to main.",
                "scope": "ctx:workspace/test_workspace",
                "applies_when": "branch is main",
                "source_authority": "system_rule",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.ERROR
    assert sink.pending_count() == 1
    assert sink.drain_valid() == []
    proposal = sink.finalize_invalid_attempts()[0]
    assert isinstance(proposal.payload, InvalidAttemptPayload)
    assert proposal.payload.attempted_kind == "ActionBoundary"


def test_remember_action_boundary_tool_valid_deposits_candidate() -> None:
    sink = MemoryProposalSink()
    tool = RememberActionBoundaryTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:1",
            name="remember_action_boundary",
            arguments={
                "statement": "Never force-push to main.",
                "scope": "ctx:workspace/test_workspace",
                "applies_when": "branch is main",
                "do_not_apply_when": "user explicitly authorizes",
                "source_authority": "system_rule",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.SUCCESS
    proposal = sink.drain()[0]
    assert isinstance(proposal.payload, ValidCandidatePayload)
    deposited = proposal.payload.candidate
    assert isinstance(deposited, ActionBoundaryCandidate)
    assert deposited.applies_when == "branch is main"
    assert deposited.do_not_apply_when == "user explicitly authorizes"


def test_remember_decision_tool_supports_based_on_ids() -> None:
    sink = MemoryProposalSink()
    tool = RememberDecisionTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:1",
            name="remember_decision",
            arguments={
                "statement": "Adopt JSON-LD for durable memory.",
                "scope": "ctx:workspace/test_project",
                "based_on_ids": ["claim:one"],
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.SUCCESS
    proposal = sink.drain()[0]
    assert isinstance(proposal.payload, ValidCandidatePayload)
    deposited = proposal.payload.candidate
    assert isinstance(deposited, DecisionCandidate)
    assert deposited.based_on_ids == ("claim:one",)


# --- AgentRuntime integration --------------------------------------------


class _ScriptedTransport:
    api = "scripted"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        reply = self.replies.pop(0)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        if "text" in reply:
            yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:1")
            yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:1", delta=reply["text"])
            yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:1")
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
            yield ToolCallEndEvent(**event_context.event_fields(), tool_call_id=call["id"])
        yield ModelCallEndEvent(**event_context.event_fields())


def _make_llm_runtime(transport: _ScriptedTransport) -> LLMRuntime:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="scripted",
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(config=config, registry=registry)


def test_agent_runtime_emits_memory_events_when_tool_proposes(tmp_path: Path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    hooks = DurableMemoryHooks(
        candidate_pool=pool,
        sink=runtime_session.memory_proposal_sink,
    )
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:propose",
                        "name": "remember_preference",
                        "arguments": json.dumps(
                            {
                                "statement": "Prefer concise summaries.",
                                "scope": "ctx:user",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(capability_runtime=CapabilityRuntime(), 
        runtime_session=runtime_session,
        llm_runtime=_make_llm_runtime(transport),
        memory_hooks=hooks,
    )

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    event_types = [event.type for event in events]
    assert EventType.MEMORY_CANDIDATE_PROPOSED not in event_types
    assert EventType.MEMORY_WRITE_RESULT not in event_types
    pending = pool.list_pending()
    assert len(pending) == 1
    assert isinstance(pending[0].payload, ValidCandidatePayload)
    assert pending[0].payload.candidate.kind == "Preference"
    assert pending[0].user_quote == "Remember that I prefer concise summaries."
    assert graph.find_by_type(memory.PREFERENCE) == []
    service = _service_on(graph)
    governance = MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=service,
        event_log=runtime_session.event_log,
        graph=graph,
        runtime_session_id=runtime_session.runtime_session_id,
        memory_write_uow_factory=fake_memory_uow_factory(
            graph=graph,
            candidate_pool=pool,
            memory_write_service=service,
        ),
    )
    governance_results = governance.submit_pending_as_is()
    result = next(e for e in governance_results[0].events if e.type is EventType.MEMORY_WRITE_RESULT)
    assert result.memory_type == "Preference"
    assert result.status is memory.NodeStatus.ACTIVE
    assert graph.has_jsonld(result.memory_id)
    # Sink is drained -- no residue after the run.
    assert runtime_session.memory_proposal_sink.pending_count() == 0
    # Memory events carry runtime-assigned sequence numbers.
    assert result.sequence is not None


def test_default_agent_runtime_does_not_expose_memory_write_tools(tmp_path: Path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:propose",
                        "name": "propose_memory",
                        "arguments": json.dumps(
                            {
                                "kind": "Preference",
                                "statement": "Prefer concise summaries.",
                                "scope": "ctx:user",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(capability_runtime=CapabilityRuntime(), 
        runtime_session=runtime_session,
        llm_runtime=_make_llm_runtime(transport),
    )

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    event_types = [event.type for event in events]
    assert EventType.MEMORY_CANDIDATE_PROPOSED not in event_types
    assert EventType.MEMORY_WRITE_RESULT not in event_types
    assert EventType.MEMORY_WRITE_FAILED not in event_types
    assert runtime_session.memory_proposal_sink.pending_count() == 0


def test_agent_runtime_invalid_proposal_emits_no_memory_events(tmp_path: Path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    hooks = DurableMemoryHooks(
        candidate_pool=pool,
        sink=runtime_session.memory_proposal_sink,
    )
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bad",
                        "name": "remember_preference",
                        "arguments": json.dumps(
                            {
                                "statement": "x",
                                "scope": "ctx:user",
                                "applies_when": "misplaced",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(capability_runtime=CapabilityRuntime(), 
        runtime_session=runtime_session,
        llm_runtime=_make_llm_runtime(transport),
        memory_hooks=hooks,
    )

    events = asyncio.run(_collect(agent, "Remember something malformed."))

    event_types = [event.type for event in events]
    assert EventType.MEMORY_CANDIDATE_PROPOSED not in event_types
    assert EventType.MEMORY_WRITE_RESULT not in event_types
    assert EventType.MEMORY_WRITE_FAILED not in event_types
    assert runtime_session.memory_proposal_sink.pending_count() == 0
    pending = pool.list_pending()
    assert len(pending) == 1
    assert isinstance(pending[0].payload, InvalidAttemptPayload)
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_agent_runtime_invalid_then_valid_same_intent_only_persists_valid(tmp_path: Path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    hooks = DurableMemoryHooks(
        candidate_pool=pool,
        sink=runtime_session.memory_proposal_sink,
    )
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bad",
                        "name": "remember_preference",
                        "arguments": json.dumps(
                            {
                                "statement": "Prefer concise summaries.",
                                "scope": "ctx:user",
                                "applies_when": "misplaced",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                            }
                        ),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:good",
                        "name": "remember_preference",
                        "arguments": json.dumps(
                            {
                                "statement": "Prefer concise summaries.",
                                "scope": "ctx:user",
                                "source_authority": "explicit_user_instruction",
                                "verification_status": "user_confirmed",
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(capability_runtime=CapabilityRuntime(), 
        runtime_session=runtime_session,
        llm_runtime=_make_llm_runtime(transport),
        memory_hooks=hooks,
    )

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    event_types = [event.type for event in events]
    assert EventType.MEMORY_CANDIDATE_PROPOSED not in event_types
    assert EventType.MEMORY_WRITE_RESULT not in event_types
    assert EventType.MEMORY_WRITE_FAILED not in event_types
    assert runtime_session.memory_proposal_sink.pending_count() == 0
    pending = pool.list_pending()
    assert len(pending) == 1
    assert isinstance(pending[0].payload, ValidCandidatePayload)
    assert pending[0].payload.candidate.statement == "Prefer concise summaries."
    assert pending[0].source_tool_call_id == "call:good"
    assert graph.find_by_type(memory.PREFERENCE) == []


async def _collect(agent: AgentRuntime, user_input: str) -> list[AgentEvent]:
    return [event async for event in agent.stream_task(user_input)]


def _proposal(candidate: PreferenceCandidate) -> CandidatePoolProposal:
    return CandidatePoolProposal(
        payload=ValidCandidatePayload(candidate=candidate),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
    )


def _invalid_proposal(call_id: str, *, statement: str) -> CandidatePoolProposal:
    return CandidatePoolProposal(
        payload=InvalidAttemptPayload(
            attempted_tool_name="remember_preference",
            attempted_kind="Preference",
            raw_arguments={
                "statement": statement,
                "scope": "ctx:user",
                "applies_when": "misplaced",
            },
            validation_error="validation failed",
        ),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
        source_tool_call_id=call_id,
    )
