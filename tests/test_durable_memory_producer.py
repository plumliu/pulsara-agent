"""Tests for the durable-memory producer: sink, DurableMemoryHooks, remember_*.

The producer bridges the agent loop to the durable-memory write path. A tool
deposits a typed candidate into a sink during tool execution; a hook drains the
sink at an agent-loop-safe point and routes each candidate through
MemoryWriteService.submit, returning the events for AgentRuntime to emit.
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
    PreferenceCandidate,
)
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.archive import InMemoryArchiveStore
from pulsara_agent.memory.durable_hooks import DurableMemoryHooks
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.memory.write_service import MemoryWriteService
from pulsara_agent.runtime import AgentRuntime, LoopState, RuntimeSession
from pulsara_agent.runtime.proposal_sink import MemoryProposalSink
from pulsara_agent.tools.base import ToolCall
from pulsara_agent.tools.builtins.memory import (
    RememberActionBoundaryTool,
    RememberDecisionTool,
    RememberPreferenceTool,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ontology import memory


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
    sink.deposit(_preference("candidate:a"))
    sink.deposit(_preference("candidate:b"))

    assert sink.pending_count() == 2
    drained = sink.drain()
    assert [c.candidate_id for c in drained] == ["candidate:a", "candidate:b"]
    assert sink.pending_count() == 0
    assert sink.drain() == []


# --- DurableMemoryHooks ---------------------------------------------------


def test_durable_hooks_drain_submits_and_returns_events() -> None:
    graph = InMemoryGraphStore()
    sink = MemoryProposalSink()
    sink.deposit(_preference())
    hooks = DurableMemoryHooks(service=_service_on(graph), sink=sink)
    state = LoopState(session_id="runtime:test")

    events = asyncio.run(hooks.after_tool_results(state, []))

    assert [event.type for event in events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_RESULT,
    ]
    result = events[1]
    assert result.candidate_id == "candidate:pref"
    assert result.memory_type == "Preference"
    assert graph.has_jsonld(result.memory_id)
    # Drained: a second drain at session end produces nothing.
    assert asyncio.run(hooks.on_session_end(state)) == []


def test_durable_hooks_empty_sink_returns_no_events() -> None:
    hooks = DurableMemoryHooks(service=_service_on(InMemoryGraphStore()), sink=MemoryProposalSink())
    state = LoopState(session_id="runtime:test")

    assert asyncio.run(hooks.after_model_reply(state, None)) == []
    assert asyncio.run(hooks.after_tool_results(state, [])) == []
    assert asyncio.run(hooks.on_session_end(state)) == []


def test_durable_hooks_event_context_comes_from_loop_state() -> None:
    sink = MemoryProposalSink()
    sink.deposit(_preference())
    hooks = DurableMemoryHooks(service=_service_on(InMemoryGraphStore()), sink=sink)
    state = LoopState(session_id="runtime:test")

    events = asyncio.run(hooks.after_tool_results(state, []))

    assert all(event.run_id == state.run_id for event in events)
    assert all(event.turn_id == state.turn_id for event in events)
    assert all(event.reply_id == state.reply_id for event in events)


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
    deposited = sink.drain()[0]
    assert isinstance(deposited, PreferenceCandidate)
    assert deposited.candidate_id.startswith("candidate:")
    assert deposited.kind == "Preference"
    assert json.loads(result.output)["status"] == "proposed"


def test_remember_preference_tool_extra_field_errors_without_deposit() -> None:
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
    assert "INVALID_CANDIDATE" in result.output
    assert sink.pending_count() == 0


def test_remember_action_boundary_tool_missing_condition_errors() -> None:
    sink = MemoryProposalSink()
    tool = RememberActionBoundaryTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:1",
            name="remember_action_boundary",
            arguments={
                "statement": "Never force-push to main.",
                "scope": "ctx:workspace",
                "applies_when": "branch is main",
                "source_authority": "system_rule",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.ERROR
    assert sink.pending_count() == 0


def test_remember_action_boundary_tool_valid_deposits_candidate() -> None:
    sink = MemoryProposalSink()
    tool = RememberActionBoundaryTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:1",
            name="remember_action_boundary",
            arguments={
                "statement": "Never force-push to main.",
                "scope": "ctx:workspace",
                "applies_when": "branch is main",
                "do_not_apply_when": "user explicitly authorizes",
                "source_authority": "system_rule",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.SUCCESS
    deposited = sink.drain()[0]
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
                "scope": "ctx:project",
                "based_on_ids": ["claim:one"],
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )

    assert result.status is ToolResultState.SUCCESS
    deposited = sink.drain()[0]
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
    runtime_session = RuntimeSession(tmp_path)
    graph = InMemoryGraphStore()
    hooks = DurableMemoryHooks(
        service=_service_on(graph),
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
    agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=_make_llm_runtime(transport),
        memory_hooks=hooks,
    )

    events = asyncio.run(_collect(agent, "Remember that I prefer concise summaries."))

    event_types = [event.type for event in events]
    assert EventType.MEMORY_CANDIDATE_PROPOSED in event_types
    assert EventType.MEMORY_WRITE_RESULT in event_types
    result = next(e for e in events if e.type is EventType.MEMORY_WRITE_RESULT)
    assert result.memory_type == "Preference"
    assert result.status is memory.NodeStatus.ACTIVE
    assert graph.has_jsonld(result.memory_id)
    # Sink is drained -- no residue after the run.
    assert runtime_session.memory_proposal_sink.pending_count() == 0
    # Memory events carry runtime-assigned sequence numbers.
    assert result.sequence is not None


def test_default_agent_runtime_does_not_expose_memory_write_tools(tmp_path: Path) -> None:
    runtime_session = RuntimeSession(tmp_path)
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
    agent = AgentRuntime(
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
    runtime_session = RuntimeSession(tmp_path)
    graph = InMemoryGraphStore()
    hooks = DurableMemoryHooks(
        service=_service_on(graph),
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
    agent = AgentRuntime(
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
    assert graph.find_by_type(memory.PREFERENCE) == []


async def _collect(agent: AgentRuntime, user_input: str) -> list[AgentEvent]:
    return [event async for event in agent.stream_task(user_input)]
