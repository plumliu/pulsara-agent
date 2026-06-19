import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    EventType,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
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
)
from pulsara_agent.llm import LLMConfig, LLMRuntime, MessageRole, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.memory import ExecutionEvidenceLedger, ExecutionEvidencePersistenceHook, InMemoryArchiveStore
from pulsara_agent.message import (
    AssistantMsg,
    Base64Source,
    DataBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.runtime import (
    AgentRuntime,
    LoopBudget,
    LoopState,
    LoopStatus,
    LoopTransition,
    RuntimeSession,
    build_tool_result_error_events,
    msg_to_llm_messages,
)
from pulsara_agent.runtime.permission import PermissionDecision, PermissionDecisionKind
from pulsara_agent.runtime.terminal import TerminalStatus
from pulsara_agent.runtime.hooks import NoopMemoryHooks
from pulsara_agent.runtime.tool_loop import _tool_result_from_event_slice
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.registry import ToolRegistry


class ScriptedTransport:
    api = "scripted"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.contexts.append(context)
        reply = self.replies.pop(0)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        if "text" in reply:
            yield TextBlockStartEvent(**event_context.event_fields(), block_id=f"text:{len(self.contexts)}")
            yield TextBlockDeltaEvent(
                **event_context.event_fields(),
                block_id=f"text:{len(self.contexts)}",
                delta=reply["text"],
            )
            yield TextBlockEndEvent(**event_context.event_fields(), block_id=f"text:{len(self.contexts)}")
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


def make_llm_runtime(transport: ScriptedTransport) -> LLMRuntime:
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


def test_loop_state_initializes_from_runtime_session(tmp_path) -> None:
    runtime_session = RuntimeSession(tmp_path)

    state = LoopState(session_id=runtime_session.runtime_session_id)
    first_turn = state.turn_id
    state.transition(LoopTransition.CONTINUE_AFTER_MODEL)
    state.begin_next_turn()

    assert state.session_id == runtime_session.runtime_session_id
    assert state.turn_index == 1
    assert state.turn_id != first_turn
    assert state.last_transition is LoopTransition.CONTINUE_AFTER_MODEL
    assert state.status is LoopStatus.RUNNING


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
                DataBlock(id="data:plot", source=Base64Source(data="abc", media_type="image/png"), name="plot"),
            ],
        ),
    ]

    llm_messages = msg_to_llm_messages(messages, LoopBudget(tool_result_context_chars=20))
    assistant_text = "\n".join(
        text
        for message in llm_messages
        if message.role is MessageRole.ASSISTANT
        for text in message.content
    )
    assistant_turn = next(
        message for message in llm_messages if message.role is MessageRole.ASSISTANT and message.tool_calls
    )
    tool_call = assistant_turn.tool_calls[0]
    tool_result = next(message for message in llm_messages if message.role is MessageRole.TOOL_RESULT)

    assert "visible" in assistant_text
    assert "hidden" not in assistant_text
    assert "call:ignored" not in assistant_text
    assert assistant_turn.thinking == ("hidden",)
    assert tool_call.id == "call:ignored"
    assert tool_call.name == "lookup"
    assert "TOOL RESULT TRUNCATED" in "\n".join(tool_result.content)
    assert tool_result.tool_call_id == "call:1"
    assert "data block omitted" in assistant_text
    assert "abc" not in assistant_text


def test_agent_runtime_finishes_text_only_reply(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("Say done"))

    assert result.status is LoopStatus.FINISHED
    assert result.stop_reason == "final"
    assert result.final_text == "done"
    assert any(event.type is EventType.TEXT_BLOCK_DELTA for event in agent.runtime_session.event_log.iter())
    assert agent.runtime_session.event_log.replay(result.state.reply_id).content[0].text == "done"


def test_agent_runtime_dispatches_event_and_completed_text_block_hooks(tmp_path) -> None:
    runtime_session = RuntimeSession(tmp_path)
    seen_events: list[EventType] = []
    seen_blocks: list[str] = []

    runtime_session.hook_manager.register_event(None, lambda context, event: seen_events.append(event.type))
    runtime_session.hook_manager.register_block(None, lambda context, completion: seen_blocks.append(completion.block_type))
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("Say done"))

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
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("Read note.txt"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "I read it."
    assert any(isinstance(event, ToolResultStartEvent) for event in agent.runtime_session.event_log.iter())
    assert len(transport.contexts) == 2
    second_context_text = "\n".join(text for msg in transport.contexts[1].messages for text in msg.content)
    assert "hello from file" in second_context_text


def test_agent_runtime_dispatches_tool_result_hooks(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hook file", encoding="utf-8")
    runtime_session = RuntimeSession(tmp_path)
    seen_tool_result_events: list[EventType] = []
    seen_tool_result_blocks: list[str] = []

    runtime_session.hook_manager.register_event(
        None,
        lambda context, event: seen_tool_result_events.append(event.type)
        if event.type.name.startswith("TOOL_RESULT")
        else None,
    )
    runtime_session.hook_manager.register_block(
        "tool_result",
        lambda context, completion: seen_tool_result_blocks.append(completion.block.output[0].text),
    )
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:read", "name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("Read note.txt"))

    assert result.status is LoopStatus.FINISHED
    assert seen_tool_result_events == [
        EventType.TOOL_RESULT_START,
        EventType.TOOL_RESULT_TEXT_DELTA,
        EventType.TOOL_RESULT_END,
    ]
    assert any("hook file" in text for text in seen_tool_result_blocks)


def test_agent_runtime_hook_error_does_not_break_run(tmp_path) -> None:
    runtime_session = RuntimeSession(tmp_path)

    def failing_hook(context, event) -> None:
        if event.type is EventType.TEXT_BLOCK_DELTA:
            raise RuntimeError("observer failed")

    runtime_session.hook_manager.register_event(None, failing_hook)
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("Say done"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "done"
    assert len(runtime_session.hook_manager.errors) == 1
    assert runtime_session.hook_manager.errors[0].message == "observer failed"


def test_tool_result_lookup_does_not_cross_runs_with_reused_tool_call_id(tmp_path) -> None:
    runtime_session = RuntimeSession(tmp_path)
    (tmp_path / "note.txt").write_text("OLD", encoding="utf-8")
    first_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:read", "name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
                ]
            },
            {"text": "first done"},
        ]
    )
    first_agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(first_transport))

    first_result = asyncio.run(first_agent.run_task("Read note.txt"))
    assert first_result.status is LoopStatus.FINISHED

    (tmp_path / "note.txt").write_text("NEW", encoding="utf-8")
    second_transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:read", "name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
                ]
            },
            {"text": "second done"},
        ]
    )
    second_agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(second_transport))

    second_result = asyncio.run(second_agent.run_task("Read note.txt again"))
    message_output = "\n".join(
        output.text
        for message in second_result.messages
        if message.role == "tool_result"
        for result in message.content
        if isinstance(result, ToolResultBlock)
        for output in result.output
        if isinstance(output, TextBlock)
    )
    second_context_text = "\n".join(text for msg in second_transport.contexts[1].messages for text in msg.content)

    assert second_result.status is LoopStatus.FINISHED
    assert "NEW" in message_output
    assert "OLD" not in message_output
    assert "NEW" in second_context_text
    assert "OLD" not in second_context_text


def test_malformed_tool_json_emits_standard_tool_result_error(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:bad", "name": "read_file", "arguments": '{"path"'}]},
            {"text": "Recovered."},
        ]
    )
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("Use a malformed tool."))
    events = agent.runtime_session.event_log.iter()
    result_events = [event for event in events if getattr(event, "tool_call_id", None) == "call:bad"]

    assert result.status is LoopStatus.FINISHED
    assert [event.type for event in result_events if event.type.name.startswith("TOOL_RESULT")] == [
        EventType.TOOL_RESULT_START,
        EventType.TOOL_RESULT_TEXT_DELTA,
        EventType.TOOL_RESULT_END,
    ]
    assert isinstance(result_events[-1], ToolResultEndEvent)
    assert result_events[-1].state is ToolResultState.ERROR
    replayed = agent.runtime_session.event_log.replay(result_events[0].reply_id)
    block = next(block for block in replayed.content if isinstance(block, ToolResultBlock))
    assert block.state is ToolResultState.ERROR
    second_context_text = "\n".join(text for msg in transport.contexts[1].messages for text in msg.content)
    assert "Malformed JSON arguments" in second_context_text


def test_malformed_tool_json_reused_id_does_not_replay_prior_error(tmp_path) -> None:
    runtime_session = RuntimeSession(tmp_path)
    first_transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:bad", "name": "read_file", "arguments": "[]"}]},
            {"text": "first recovered"},
        ]
    )
    first_agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(first_transport))
    asyncio.run(first_agent.run_task("bad first"))

    second_transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:bad", "name": "read_file", "arguments": '{"second"'}]},
            {"text": "second recovered"},
        ]
    )
    second_agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(second_transport))

    second_result = asyncio.run(second_agent.run_task("bad second"))
    message_output = "\n".join(
        output.text
        for message in second_result.messages
        if message.role == "tool_result"
        for result in message.content
        if isinstance(result, ToolResultBlock)
        for output in result.output
        if isinstance(output, TextBlock)
    )
    second_context_text = "\n".join(text for msg in second_transport.contexts[1].messages for text in msg.content)

    assert "Malformed JSON arguments" in message_output
    assert "must be a JSON object" not in message_output
    assert "Malformed JSON arguments" in second_context_text
    assert "must be a JSON object" not in second_context_text


def test_unknown_tool_becomes_error_observation(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:missing", "name": "missing_tool", "arguments": "{}"}]},
            {"text": "Recovered from missing tool."},
        ]
    )
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("Call a missing tool."))
    second_context_text = "\n".join(text for msg in transport.contexts[1].messages for text in msg.content)

    assert result.status is LoopStatus.FINISHED
    assert "Unknown tool: missing_tool" in second_context_text
    assert any(
        isinstance(event, ToolResultEndEvent) and event.tool_call_id == "call:missing" and event.state is ToolResultState.ERROR
        for event in agent.runtime_session.event_log.iter()
    )


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
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        budget=LoopBudget(max_turns=1),
    )

    result = asyncio.run(agent.run_task("Read forever."))

    assert result.status is LoopStatus.FAILED
    assert result.stop_reason == "max_turns"
    assert any(event.type is EventType.EXCEED_MAX_ITERS for event in agent.runtime_session.event_log.iter())


def test_build_tool_result_error_events_use_standard_event_shape() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    event_log = InMemoryEventLog()
    context = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")

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
    runtime_session = RuntimeSession(tmp_path)
    first_transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:deny", "name": "read_file", "arguments": json.dumps({"path": "x"})}]},
            {"text": "first recovered"},
        ]
    )
    first_agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(first_transport),
        permission_gate=DenyGate("FIRST_DENY"),
    )
    asyncio.run(first_agent.run_task("deny first"))

    second_transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:deny", "name": "read_file", "arguments": json.dumps({"path": "x"})}]},
            {"text": "second recovered"},
        ]
    )
    second_agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=make_llm_runtime(second_transport),
        permission_gate=DenyGate("SECOND_DENY"),
    )

    second_result = asyncio.run(second_agent.run_task("deny second"))
    message_output = "\n".join(
        output.text
        for message in second_result.messages
        if message.role == "tool_result"
        for result in message.content
        if isinstance(result, ToolResultBlock)
        for output in result.output
        if isinstance(output, TextBlock)
    )
    second_context_text = "\n".join(text for msg in second_transport.contexts[1].messages for text in msg.content)

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
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))

    result = asyncio.run(agent.run_task("attempt dangerous command"))
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    confirm = next(event for event in events if isinstance(event, RequireUserConfirmEvent))

    assert result.status is LoopStatus.WAITING_USER
    assert result.stop_reason == "waiting_user"
    assert confirm.tool_calls[0].id == "call:danger"
    assert confirm.tool_calls[0].name == "terminal"
    assert confirm.tool_calls[0].state is ToolCallState.ASKING
    assert confirm.tool_calls[0].suggested_rules[0]["reason"] == "dangerous_terminal_command"
    assert not any(isinstance(event, ToolResultStartEvent) for event in events)


def test_agent_runtime_finished_run_keeps_background_process_until_session_close(tmp_path) -> None:
    runtime_session = RuntimeSession(tmp_path)
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bg",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "sleep 10", "yield_time_ms": 0}),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(transport))
    process_id: str | None = None

    try:
        result = asyncio.run(agent.run_task("start background then finish"))
        tool_delta = next(
            event
            for event in runtime_session.event_log.iter(run_id=result.state.run_id)
            if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:bg"
        )
        process_id = json.loads(tool_delta.delta)["process_id"]

        assert result.status is LoopStatus.FINISHED
        assert runtime_session.terminal_sessions.poll_process(process_id).status is TerminalStatus.RUNNING
    finally:
        runtime_session.close()

    if process_id is not None:
        assert runtime_session.terminal_sessions.poll_process(process_id).status is TerminalStatus.KILLED


def test_tool_result_from_event_slice_folds_text_and_data_blocks() -> None:
    context = EventContext(run_id="run:slice", turn_id="turn:slice", reply_id="reply:slice")
    events = [
        ToolResultStartEvent(**context.event_fields(), tool_call_id="call:slice", tool_call_name="lookup"),
        ToolResultTextDeltaEvent(**context.event_fields(), tool_call_id="call:slice", delta="hello "),
        ToolResultTextDeltaEvent(**context.event_fields(), tool_call_id="call:slice", delta="world"),
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
    context = EventContext(run_id="run:slice", turn_id="turn:slice", reply_id="reply:slice")

    try:
        _tool_result_from_event_slice([], "call:missing")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for missing tool result start")

    for events in [
        [ToolResultTextDeltaEvent(**context.event_fields(), tool_call_id="call:bad", delta="orphan")],
        [ToolResultEndEvent(**context.event_fields(), tool_call_id="call:bad", state=ToolResultState.ERROR)],
        [ToolResultStartEvent(**context.event_fields(), tool_call_id="call:bad", tool_call_name="lookup")],
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

    async def project(self, state: LoopState, *, token_budget: int):
        self.calls.append("project")
        return {"summary": "Remember source=fenced.", "included_memory_ids": ["mem:1"]}

    async def after_model_reply(self, state: LoopState, assistant):
        self.calls.append("after_model")

    async def after_tool_results(self, state: LoopState, results):
        self.calls.append("after_tools")

    async def on_session_end(self, state: LoopState) -> None:
        self.calls.append("end")


class SlowProjectionHooks(NoopMemoryHooks):
    async def project(self, state: LoopState, *, token_budget: int):
        await asyncio.sleep(0.05)
        return {"summary": "too late", "included_memory_ids": ["mem:late"]}


def test_memory_hooks_and_projection_events_are_used(tmp_path) -> None:
    hooks = RecordingHooks()
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=hooks,
    )

    asyncio.run(agent.run_task("hi"))

    assert hooks.calls == ["start", "project", "after_model", "end"]
    events = agent.runtime_session.event_log.iter()
    assert any(event.type is EventType.PROJECTION_REQUESTED for event in events)
    assert any(event.type is EventType.PROJECTION_READY for event in events)
    assert "Recalled Memory" in (transport.contexts[0].system_prompt or "")


def test_memory_projection_timeout_fails_soft_without_blocking_reply(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=SlowProjectionHooks(),
        budget=LoopBudget(recall_hard_timeout_ms=1),
    )

    result = asyncio.run(agent.run_task("hi"))

    assert result.status is LoopStatus.FINISHED
    assert result.final_text == "done"
    assert result.state.memory_projection is None
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    failed = next(event for event in events if event.type is EventType.PROJECTION_FAILED)
    assert failed.error == "recall_timeout"
    assert "Recalled Memory" not in (transport.contexts[0].system_prompt or "")


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
    async def after_tool_results(self, state: LoopState, results: list[ToolResultBlock]) -> None:
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
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("on_session_start"),
    )

    result = asyncio.run(agent.run_task("hi"))

    _assert_memory_hook_failed(agent, result, "on_session_start")
    assert transport.contexts == []


def test_memory_hook_failure_after_model_reply_returns_failed_result(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("after_model_reply"),
    )

    result = asyncio.run(agent.run_task("hi"))

    _assert_memory_hook_failed(agent, result, "after_model_reply")
    assert result.final_text == "done"


def test_memory_hook_event_emit_failure_returns_failed_result(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=InvalidEventHook(),
    )

    result = asyncio.run(agent.run_task("hi"))

    _assert_memory_hook_failed(agent, result, "after_model_reply")
    assert result.final_text == "done"


def test_agent_runtime_accepts_memory_hook_without_proposal_sink_property(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=LegacyShapeMemoryHook(),
    )

    result = asyncio.run(agent.run_task("hi"))

    assert result.status is LoopStatus.FINISHED
    assert "propose_memory" not in agent.tool_executor.registry.names()
    assert not any(name.startswith("remember_") for name in agent.tool_executor.registry.names())


def test_memory_hook_failure_after_tool_results_returns_failed_result(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:read", "name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
                ]
            },
            {"text": "should not run"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("after_tool_results"),
    )

    result = asyncio.run(agent.run_task("read"))

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
                    {"id": "call:read", "name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        tool_result_persistence_hook=ExecutionEvidencePersistenceHook(ledger),
    )

    result = asyncio.run(agent.run_task("read"))

    assert result.status is LoopStatus.FINISHED
    tool_results = graph.find_by_type(rt.TOOL_RESULT)
    assert len(tool_results) == 1
    assert graph.find_by_type(rt.EVIDENCE) == []
    assert graph.find_by_type(memory.CLAIM) == []
    span = tool_results[0][rt.EVENT_SPAN_PROPERTY.name]
    assert span[rt.SOURCE_SESSION.name] == agent.runtime_session.runtime_session_id


def test_tool_result_persistence_hook_failure_does_not_break_run(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:read", "name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        tool_result_persistence_hook=FailingPersistenceHook(),
    )

    result = asyncio.run(agent.run_task("read"))
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)

    assert result.status is LoopStatus.FINISHED
    assert result.stop_reason == "final"
    assert any(event.type is EventType.CUSTOM and event.name == "tool_result_persistence_failed" for event in events)
    assert not any(isinstance(event, RunErrorEvent) and event.code == "memory_persistence_error" for event in events)
    assert any(isinstance(event, RunEndEvent) and event.status == "finished" for event in events)


def test_memory_hook_failure_should_compact_returns_failed_result(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {"id": "call:read", "name": "read_file", "arguments": json.dumps({"path": "note.txt"})}
                ]
            },
            {"text": "should not run"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("should_compact"),
    )

    result = asyncio.run(agent.run_task("read"))

    _assert_memory_hook_failed(agent, result, "should_compact")
    assert len(transport.contexts) == 1


def test_memory_hook_failure_on_session_end_returns_failed_result(tmp_path) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        memory_hooks=FailingHook("on_session_end"),
    )

    result = asyncio.run(agent.run_task("hi"))

    _assert_memory_hook_failed(agent, result, "on_session_end")
    assert result.final_text == "done"


@dataclass(slots=True)
class SleepTool:
    name: str
    delay: float
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    description: str = "Sleep briefly."
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        time.sleep(self.delay)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=call.name,
        )


@dataclass(slots=True)
class RecordingTool:
    name: str
    calls: list[str]
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    description: str = "Record execution."
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

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
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

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
    runtime_session = RuntimeSession(tmp_path)
    registry = ToolRegistry()
    registry.register(BlockingUntilStartHookTool(release=release))
    transport = ScriptedTransport(
        [
            {"tool_calls": [{"id": "call:block", "name": "blocking_tool", "arguments": "{}"}]},
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(transport))
    agent.tool_executor.registry = registry

    def release_on_start(context, event) -> None:
        if isinstance(event, ToolResultStartEvent) and event.tool_call_id == "call:block":
            release.set()

    runtime_session.hook_manager.register_event(EventType.TOOL_RESULT_START, release_on_start)

    result = asyncio.run(agent.run_task("run blocking tool"))

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


def test_duplicate_tool_call_id_becomes_error_observation_without_execution(tmp_path) -> None:
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
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))
    registry = ToolRegistry()
    registry.register(RecordingTool("dup_tool", calls=calls, is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("run duplicate tool ids"))
    second_context_text = "\n".join(text for msg in transport.contexts[1].messages for text in msg.content)

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
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))
    registry = ToolRegistry()
    registry.register(RecordingTool("ok_tool", calls=calls, is_read_only=True, is_concurrency_safe=True))
    registry.register(RecordingTool("dup_tool", calls=calls, is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("run mixed duplicate tool ids"))
    second_context_text = "\n".join(text for msg in transport.contexts[1].messages for text in msg.content)

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
            {"tool_calls": [{"id": "call:write", "name": "write_side_effect", "arguments": "{}"}]},
            {"text": "should not run"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        budget=LoopBudget(max_tool_calls=0),
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("write_side_effect", calls=calls))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("run unsafe tool"))
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
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=make_llm_runtime(transport),
        budget=LoopBudget(max_tool_calls=1),
    )
    registry = ToolRegistry()
    registry.register(RecordingTool("readonly_a", calls=calls, is_read_only=True, is_concurrency_safe=True))
    registry.register(RecordingTool("readonly_b", calls=calls, is_read_only=True, is_concurrency_safe=True))
    agent.tool_executor.registry = registry

    result = asyncio.run(agent.run_task("run readonly tools"))
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
    agent = AgentRuntime(runtime_session=RuntimeSession(tmp_path), llm_runtime=make_llm_runtime(transport))
    registry = ToolRegistry()
    registry.register(SleepTool("sleep_a", delay=0.2))
    registry.register(SleepTool("sleep_b", delay=0.2))
    agent.tool_executor.registry = registry

    started = time.monotonic()
    asyncio.run(agent.run_task("run both"))
    elapsed = time.monotonic() - started
    sequences = [event.sequence for event in agent.runtime_session.event_log.iter()]

    assert elapsed < 0.35
    assert sequences == sorted(sequences)


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
    runtime_session = RuntimeSession(tmp_path)
    agent = AgentRuntime(runtime_session=runtime_session, llm_runtime=make_llm_runtime(transport))
    registry = ToolRegistry()
    registry.register(SleepTool("sleep_a", delay=0.2))
    registry.register(SleepTool("sleep_b", delay=0.2))
    agent.tool_executor.registry = registry
    seen_sequences: list[int] = []

    def record_tool_result_sequences(context, event) -> None:
        if event.type.name.startswith("TOOL_RESULT") and event.sequence is not None:
            seen_sequences.append(event.sequence)

    runtime_session.hook_manager.register_event(None, record_tool_result_sequences)

    result = asyncio.run(agent.run_task("run both"))

    assert result.status is LoopStatus.FINISHED
    assert seen_sequences == sorted(seen_sequences)
