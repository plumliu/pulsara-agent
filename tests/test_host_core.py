import asyncio
import json
from typing import AsyncIterator

import pytest

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.host import HostCore, HostSessionBusyError, HostWorkspaceInput
from pulsara_agent.host.transcript import FAILURE_NOTE_TEXT, rebuild_prior_messages
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile, ModelRole
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.runtime.terminal import TerminalStatus
from pulsara_agent.settings import PulsaraSettings, StorageConfig


class ScriptedTransport:
    api = "scripted"

    def __init__(self, replies: list[dict], *, delay: float = 0) -> None:
        self.replies = replies
        self.delay = delay
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
        if self.delay:
            await asyncio.sleep(self.delay)
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


class FailingScriptedTransport:
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
            if reply.get("close_text_block", True):
                yield TextBlockEndEvent(**event_context.event_fields(), block_id=f"text:{len(self.contexts)}")
        if "run_error" in reply:
            error = reply["run_error"]
            yield RunErrorEvent(
                **event_context.event_fields(),
                message=error["message"],
                code=error["code"],
                metadata=error.get("metadata", {}),
            )
            return
        if "raise" in reply:
            raise reply["raise"]
        yield ModelCallEndEvent(**event_context.event_fields())


def _settings() -> PulsaraSettings:
    return PulsaraSettings(
        llm=LLMConfig(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="scripted",
        ),
        storage=StorageConfig(postgres_dsn="", oxigraph_url="http://127.0.0.1:1"),
    )


def _core(monkeypatch, transport: ScriptedTransport, *, use_workspace_supervisor: bool = True) -> HostCore:
    settings = _settings()
    registry = LLMTransportRegistry()
    registry.register(transport)
    core = HostCore(settings=settings, durable=False, use_workspace_supervisor=use_workspace_supervisor)

    def _patched_runtime(_config):
        return LLMRuntime(config=settings.llm, registry=registry)

    import pulsara_agent.runtime.wiring as wiring

    monkeypatch.setattr(wiring, "build_llm_runtime", _patched_runtime)
    return core


async def _open_project_session(core: HostCore, tmp_path, *, host_session_id: str = "host:test"):
    return await core.open_session(
        HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path, memory_domain_id="u_test"),
        host_session_id=host_session_id,
        conversation_id=f"conversation:{host_session_id}",
        model_role=ModelRole.FLASH,
        memory_reflection=False,
    )


def _context_text(context: LLMContext) -> str:
    return "\n".join(part for message in context.messages for part in message.content)


def test_host_session_seeds_next_turn_from_event_log(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "sentinel-one"}, {"text": "sentinel-two"}])
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        await session.run_turn("first user")
        await session.run_turn("second user")
        return session

    session = asyncio.run(run())

    assert session.runtime_session_id == session.wiring.agent_runtime.runtime_session.runtime_session_id
    assert "first user" in _context_text(transport.contexts[1])
    assert "sentinel-one" in _context_text(transport.contexts[1])
    assert FAILURE_NOTE_TEXT not in _context_text(transport.contexts[1])


def test_rebuild_prior_messages_injects_system_note_for_failed_last_run_with_reply_end() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:failed", turn_id="turn:failed", reply_id="reply:failed")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "first user"}),
            ModelCallStartEvent(**ctx.event_fields(), model_name="flash", model_role="flash", provider="scripted"),
            RunErrorEvent(
                **ctx.event_fields(),
                message="APIConnectionError: sk-secret https://api.deepseek.com retry trace",
                code="openai_responses_error",
                metadata={"provider_data": {"base_url": "https://api.deepseek.com", "api_key": "sk-secret"}},
            ),
            ReplyEndEvent(**ctx.event_fields()),
            RunEndEvent(
                **ctx.event_fields(),
                status="failed",
                stop_reason="model_error",
                error_message="model error budget exceeded with sk-secret",
            ),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert messages[0].role == "user"
    assert messages[0].content[0].text == "first user"
    assert messages[1].role == "assistant"
    assert messages[1].content == []
    assert messages[2].role == "system"
    assert messages[2].content[0].text == FAILURE_NOTE_TEXT
    rendered = "\n".join(
        getattr(block, "text", "") for message in messages for block in getattr(message, "content", [])
    )
    assert "sk-secret" not in rendered
    assert "api.deepseek.com" not in rendered
    assert "retry trace" not in rendered


def test_rebuild_prior_messages_keeps_partial_reply_before_failure_note() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:partial", turn_id="turn:partial", reply_id="reply:partial")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "first user"}),
            TextBlockStartEvent(**ctx.event_fields(), block_id="text:1"),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="partial answer"),
            RunErrorEvent(**ctx.event_fields(), message="provider failed", code="openai_responses_error"),
            ReplyEndEvent(**ctx.event_fields()),
            RunEndEvent(**ctx.event_fields(), status="failed", stop_reason="model_error"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert messages[1].role == "assistant"
    assert messages[1].content[0].text == "partial answer"
    assert messages[2].role == "system"
    assert messages[2].content[0].text == FAILURE_NOTE_TEXT


def test_rebuild_prior_messages_does_not_inject_note_when_newer_run_succeeds() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    failed_ctx = EventContext(run_id="run:failed", turn_id="turn:failed", reply_id="reply:failed")
    done_ctx = EventContext(run_id="run:done", turn_id="turn:done", reply_id="reply:done")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**failed_ctx.event_fields(), user_input_chars=10, metadata={"user_input": "failed user"}),
            RunErrorEvent(**failed_ctx.event_fields(), message="provider failed", code="openai_responses_error"),
            ReplyEndEvent(**failed_ctx.event_fields()),
            RunEndEvent(**failed_ctx.event_fields(), status="failed", stop_reason="model_error"),
            RunStartEvent(**done_ctx.event_fields(), user_input_chars=8, metadata={"user_input": "done user"}),
            TextBlockStartEvent(**done_ctx.event_fields(), block_id="text:done"),
            TextBlockDeltaEvent(**done_ctx.event_fields(), block_id="text:done", delta="done"),
            TextBlockEndEvent(**done_ctx.event_fields(), block_id="text:done"),
            ReplyEndEvent(**done_ctx.event_fields()),
            RunEndEvent(**done_ctx.event_fields(), status="finished", stop_reason="final"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "assistant", "user", "assistant"]
    assert all(
        not (message.role == "system" and message.content and message.content[0].text == FAILURE_NOTE_TEXT)
        for message in messages
    )


def test_rebuild_prior_messages_injects_note_for_failed_last_run_without_reply_end() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:raised", turn_id="turn:raised", reply_id="reply:raised")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "first user"}),
            RunErrorEvent(**ctx.event_fields(), message="APIConnectionError: boom", code="model_stream_error"),
            RunEndEvent(**ctx.event_fields(), status="failed", stop_reason="model_error"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "system"]
    assert messages[1].content[0].text == FAILURE_NOTE_TEXT


def test_host_session_injects_failed_turn_note_into_next_context(tmp_path, monkeypatch) -> None:
    transport = FailingScriptedTransport(
        [
            {"run_error": {"message": "APIConnectionError: sk-secret", "code": "openai_responses_error"}},
            {"run_error": {"message": "APIConnectionError: sk-secret", "code": "openai_responses_error"}},
            {"run_error": {"message": "APIConnectionError: sk-secret", "code": "openai_responses_error"}},
            {"text": "recovered"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        await session.run_turn("first user")
        await session.run_turn("please continue")
        return session

    session = asyncio.run(run())

    assert session.runtime_session_id == session.wiring.agent_runtime.runtime_session.runtime_session_id
    assert len(transport.contexts) == 4
    second_context = transport.contexts[-1]
    assert any(message.role.value == "system" and FAILURE_NOTE_TEXT in "\n".join(message.content) for message in second_context.messages)
    assert "first user" in _context_text(second_context)
    assert "please continue" in _context_text(second_context)
    assert "sk-secret" not in _context_text(second_context)


def test_host_session_replay_events_after_sequence(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "hello"}])
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open_project_session(core, tmp_path)
        await session.run_turn("hi")
        return session

    session = asyncio.run(run())
    all_events = session.replay_events()
    missed = session.replay_events(after_sequence=2)

    assert [event.sequence for event in missed] == [seq for seq in [event.sequence for event in all_events] if seq > 2]


def test_host_session_rejects_concurrent_runs(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "slow"}, {"text": "fast"}], delay=0.05)
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open_project_session(core, tmp_path)
        first = asyncio.create_task(session.run_turn("first"))
        await asyncio.sleep(0)
        with pytest.raises(HostSessionBusyError):
            await session.run_turn("second")
        return await first

    result = asyncio.run(run())

    assert result.final_text == "slow"


def test_host_core_reconnect_by_session_id_returns_same_session(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open_project_session(core, tmp_path, host_session_id="host:reconnect")
        same = await core.get_session("host:reconnect")
        by_conversation = await core.find_by_conversation("conversation:host:reconnect")
        return session, same, by_conversation

    session, same, by_conversation = asyncio.run(run())

    assert same is session
    assert by_conversation is session


def test_host_core_lists_workspace_supervisor_diagnostics(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open_project_session(core, tmp_path, host_session_id="host:diagnostics")
        summaries = await core.list_workspace_supervisors()
        await core.close_session(session.host_session_id)
        return summaries

    summaries = asyncio.run(run())

    assert summaries[0]["workspace_root"] == str(tmp_path.resolve())
    assert summaries[0]["owner_session_count"] == 1
    assert summaries[0]["live_process_count"] == 0


def test_idle_sweep_marks_live_process_session_without_closing(tmp_path, monkeypatch) -> None:
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
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open_project_session(core, tmp_path, host_session_id="host:idle")
        await session.run_turn("start process")
        session.last_active_at -= 30_000
        closed = await core.registry.sweep_idle()
        summaries = await core.list_sessions()
        await core.close_session(session.host_session_id)
        return closed, summaries

    closed, summaries = asyncio.run(run())

    assert closed == []
    assert summaries[0].idle_with_live_processes is True


def test_workspace_supervisor_owner_isolation_and_workspace_shutdown(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:a",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "sleep 10", "yield_time_ms": 0}),
                    }
                ]
            },
            {"text": "a done"},
            {
                "tool_calls": [
                    {
                        "id": "call:b",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "sleep 10", "yield_time_ms": 0}),
                    }
                ]
            },
            {"text": "b done"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        a = await _open_project_session(core, tmp_path, host_session_id="host:a")
        b = await _open_project_session(core, tmp_path, host_session_id="host:b")
        await a.run_turn("start a")
        await b.run_turn("start b")
        manager = a.wiring.runtime_wiring.runtime_session.terminal_sessions
        assert manager is b.wiring.runtime_wiring.runtime_session.terminal_sessions
        a_proc = manager.list_owned("host:a")[0].process_id
        b_proc = manager.list_owned("host:b")[0].process_id
        assert a_proc is not None and b_proc is not None
        with pytest.raises(KeyError):
            manager.poll_process(a_proc, owner_host_session_id="host:b")
        await core.close_session("host:a")
        a_status = manager.poll_process(a_proc).status
        b_status = manager.poll_process(b_proc).status
        await core.close_workspace(a.workspace.workspace_key)
        b_status_after_workspace_close = manager.poll_process(b_proc).status
        remaining_sessions = await core.list_sessions()
        return a_status, b_status, b_status_after_workspace_close, remaining_sessions

    a_status, b_status, b_status_after_workspace_close, remaining_sessions = asyncio.run(run())

    assert a_status is TerminalStatus.KILLED
    assert b_status is TerminalStatus.RUNNING
    assert b_status_after_workspace_close is TerminalStatus.KILLED
    assert remaining_sessions == []


def test_host_session_default_terminal_cwd_is_owner_scoped(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:a",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "mkdir -p src && cd src && pwd"}),
                    }
                ]
            },
            {"text": "a done"},
            {
                "tool_calls": [
                    {
                        "id": "call:b",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "pwd"}),
                    }
                ]
            },
            {"text": "b done"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        a = await _open_project_session(core, tmp_path, host_session_id="host:cwd-a")
        b = await _open_project_session(core, tmp_path, host_session_id="host:cwd-b")
        await a.run_turn("cd in a")
        await b.run_turn("pwd in b")
        a_terminal = a.wiring.runtime_wiring.runtime_session.terminal_sessions.get_or_create(
            owner_host_session_id="host:cwd-a"
        )
        b_terminal = b.wiring.runtime_wiring.runtime_session.terminal_sessions.get_or_create(
            owner_host_session_id="host:cwd-b"
        )
        await core.shutdown()
        return a_terminal.current_cwd, b_terminal.current_cwd

    a_cwd, b_cwd = asyncio.run(run())

    assert a_cwd == tmp_path / "src"
    assert b_cwd == tmp_path


def test_host_core_transient_uses_distinct_workspace_supervisor(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "one"}, {"text": "two"}])
    core = _core(monkeypatch, transport)

    async def run():
        first = await core.open_session(
            HostWorkspaceInput(workspace_kind="transient", workspace_root=tmp_path / "s1"),
            host_session_id="host:t1",
            conversation_id="conversation:t1",
            model_role=ModelRole.FLASH,
            memory_reflection=False,
        )
        second = await core.open_session(
            HostWorkspaceInput(workspace_kind="transient", workspace_root=tmp_path / "s2"),
            host_session_id="host:t2",
            conversation_id="conversation:t2",
            model_role=ModelRole.FLASH,
            memory_reflection=False,
        )
        return first.workspace.workspace_key, second.workspace.workspace_key

    first_key, second_key = asyncio.run(run())

    assert first_key != second_key


def test_host_core_keeps_auto_transient_root_on_close_by_default(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    core = _core(monkeypatch, transport)

    async def run():
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="transient"),
            host_session_id="host:auto-transient",
            conversation_id="conversation:auto-transient",
            model_role=ModelRole.FLASH,
            memory_reflection=False,
        )
        root = session.workspace.workspace_root
        marker = root / "marker.txt"
        marker.write_text("scratch", encoding="utf-8")
        assert root.exists()
        await core.close_session(session.host_session_id)
        return root

    root = asyncio.run(run())

    assert root.exists()
    assert (root / "marker.txt").read_text(encoding="utf-8") == "scratch"


def test_host_core_removes_auto_transient_root_on_close_when_requested(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    core = _core(monkeypatch, transport)

    async def run():
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="transient",
                cleanup_workspace_root_on_close=True,
            ),
            host_session_id="host:auto-transient-cleanup",
            conversation_id="conversation:auto-transient-cleanup",
            model_role=ModelRole.FLASH,
            memory_reflection=False,
        )
        root = session.workspace.workspace_root
        marker = root / "marker.txt"
        marker.write_text("scratch", encoding="utf-8")
        assert root.exists()
        await core.close_session(session.host_session_id)
        return root

    root = asyncio.run(run())

    assert not root.exists()


def test_host_core_keeps_host_supplied_transient_root_on_close(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    core = _core(monkeypatch, transport)
    supplied_root = tmp_path / "supplied"

    async def run():
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="transient", workspace_root=supplied_root),
            host_session_id="host:supplied-transient",
            conversation_id="conversation:supplied-transient",
            model_role=ModelRole.FLASH,
            memory_reflection=False,
        )
        marker = session.workspace.workspace_root / "marker.txt"
        marker.write_text("host-owned", encoding="utf-8")
        await core.close_session(session.host_session_id)

    asyncio.run(run())

    assert supplied_root.exists()
    assert (supplied_root / "marker.txt").read_text(encoding="utf-8") == "host-owned"


def test_host_core_keeps_project_root_on_close(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "done"}])
    core = _core(monkeypatch, transport)
    marker = tmp_path / "marker.txt"
    marker.write_text("project", encoding="utf-8")

    async def run():
        session = await _open_project_session(core, tmp_path, host_session_id="host:project-close")
        await core.close_session(session.host_session_id)

    asyncio.run(run())

    assert tmp_path.exists()
    assert marker.read_text(encoding="utf-8") == "project"
