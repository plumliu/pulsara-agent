import asyncio
import json
from typing import AsyncIterator

import pytest

from pulsara_agent.event import (
    AgentEvent,
    ConfirmResult,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ReplyEndEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    TerminalProcessCompletedEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.host import (
    HostCore,
    HostSessionBusyError,
    HostSessionPendingApprovalError,
    HostSessionPendingInteractionError,
    HostWorkspaceInput,
)
from pulsara_agent.host.transcript import FAILURE_NOTE_TEXT, INTERRUPTED_NOTE_TEXT, rebuild_prior_messages
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile, ModelRole
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.message import ToolCallBlock, ToolCallState, ToolResultBlock, ToolResultState
from pulsara_agent.message.message import AssistantMsg
from pulsara_agent.message.reducer import MessageReducer
from pulsara_agent.runtime import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.plan import PendingPlanInteraction, PlanExitResolution, PlanQuestionResolution
from pulsara_agent.runtime.state import LoopBudget
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionMode,
    PermissionProfile,
    TerminalAccess,
    preset_to_policy,
)
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
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


def _trusted_terminal_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.RISKY_ONLY,
        terminal=TerminalAccess.ALLOW,
    )


def _trusted_terminal_ask_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.RISKY_ONLY,
        terminal=TerminalAccess.ASK,
    )


def _workspace_on_request_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.WORKSPACE_GUARDED,
        approval=ApprovalPolicy.ON_REQUEST,
        terminal=TerminalAccess.OFF,
    )


async def _open_project_session(
    core: HostCore,
    tmp_path,
    *,
    host_session_id: str = "host:test",
    permission_policy: EffectivePermissionPolicy | None = None,
):
    return await core.open_session(
        HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path, memory_domain_id="u_test"),
        host_session_id=host_session_id,
        conversation_id=f"conversation:{host_session_id}",
        model_role=ModelRole.FLASH,
        memory_reflection=False,
        permission_policy=permission_policy,
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


def test_rebuild_prior_messages_injects_system_note_for_aborted_last_run() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:aborted", turn_id="turn:aborted", reply_id="reply:aborted")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "long user task"}),
            TextBlockStartEvent(**ctx.event_fields(), block_id="text:1"),
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="partial answer"),
            ReplyEndEvent(**ctx.event_fields()),
            RunEndEvent(**ctx.event_fields(), status="aborted", stop_reason="aborted"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "assistant", "system"]
    assert messages[1].content[0].text == "partial answer"
    assert messages[2].content[0].text == INTERRUPTED_NOTE_TEXT
    assert messages[2].metadata == {"run_id": "run:aborted", "kind": "previous_turn_aborted"}


def test_rebuild_prior_messages_uses_plan_aborted_note_when_plan_remains_active() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:plan-aborted", turn_id="turn:plan-aborted", reply_id="reply:plan-aborted")
    log = InMemoryEventLog()
    log.extend(
        [
            PlanModeEnteredEvent(
                **ctx.event_fields(),
                source="user",
                previous_permission_mode="bypass-permissions",
                previous_permission_policy={"profile": "trusted_host"},
                reason="plan first",
            ),
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "ask plan question"}),
            ReplyEndEvent(**ctx.event_fields()),
            RunEndEvent(
                **ctx.event_fields(),
                status="aborted",
                stop_reason="aborted",
                abort_kind="user_stop",
            ),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "assistant", "system"]
    assert "plan workflow turn was stopped by the user" in messages[2].content[0].text
    assert "Planning remains active and read-only" in messages[2].content[0].text


def test_rebuild_prior_messages_strips_unfinished_tool_call_from_aborted_run() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:aborted", turn_id="turn:aborted", reply_id="reply:aborted")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "dangerous task"}),
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:danger", tool_call_name="terminal"),
            ToolCallDeltaEvent(**ctx.event_fields(), tool_call_id="call:danger", delta='{"command": "rm -rf ./x"}'),
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:danger"),
            RequireUserConfirmEvent(
                **ctx.event_fields(),
                tool_calls=[ToolCallBlock(id="call:danger", name="terminal", input='{"command": "rm -rf ./x"}')],
            ),
            ReplyEndEvent(**ctx.event_fields()),
            RunEndEvent(**ctx.event_fields(), status="aborted", stop_reason="aborted"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "system"]
    note = messages[1].content[0].text
    assert note.startswith(INTERRUPTED_NOTE_TEXT)
    assert "terminal" in note
    assert "pending approval and did not execute" in note
    assert "rm -rf" not in note


def test_rebuild_prior_messages_note_mentions_started_terminal_without_completed_result() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:failed", turn_id="turn:failed", reply_id="reply:failed")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "run command"}),
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
            ToolCallDeltaEvent(**ctx.event_fields(), tool_call_id="call:terminal", delta='{"command": "sleep 30"}'),
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:terminal"),
            ToolResultStartEvent(**ctx.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
            RunEndEvent(**ctx.event_fields(), status="failed", stop_reason="tool_error"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "system"]
    note = messages[1].content[0].text
    assert note.startswith(FAILURE_NOTE_TEXT)
    assert "terminal" in note
    assert "may have partially run and may still be running in the background" in note
    assert "sleep 30" not in note


def test_rebuild_prior_messages_late_tool_result_removes_unfinished_summary() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:aborted", turn_id="turn:aborted", reply_id="reply:aborted")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "run command"}),
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
            ToolCallDeltaEvent(**ctx.event_fields(), tool_call_id="call:terminal", delta='{"command": "printf done"}'),
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:terminal"),
            ReplyEndEvent(**ctx.event_fields()),
            ToolResultStartEvent(**ctx.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
            RunEndEvent(**ctx.event_fields(), status="aborted", stop_reason="aborted"),
            ToolResultTextDeltaEvent(**ctx.event_fields(), tool_call_id="call:terminal", delta="done"),
            ToolResultEndEvent(**ctx.event_fields(), tool_call_id="call:terminal", state=ToolResultState.SUCCESS),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "assistant", "system"]
    assistant_blocks = messages[1].content
    assert any(isinstance(block, ToolCallBlock) and block.id == "call:terminal" for block in assistant_blocks)
    assert any(isinstance(block, ToolResultBlock) and block.id == "call:terminal" for block in assistant_blocks)
    assert messages[2].content[0].text == INTERRUPTED_NOTE_TEXT


def test_rebuild_prior_messages_note_mentions_failed_proposed_only_tools() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:failed", turn_id="turn:failed", reply_id="reply:failed")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "change files"}),
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
            ToolCallDeltaEvent(**ctx.event_fields(), tool_call_id="call:write", delta='{"path": "secret.txt"}'),
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:write"),
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:term", tool_call_name="terminal"),
            ToolCallDeltaEvent(**ctx.event_fields(), tool_call_id="call:term", delta='{"command": "echo hidden"}'),
            ToolCallEndEvent(**ctx.event_fields(), tool_call_id="call:term"),
            RunEndEvent(**ctx.event_fields(), status="failed", stop_reason="model_error"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "system"]
    note = messages[1].content[0].text
    assert note.startswith(FAILURE_NOTE_TEXT)
    assert "write_file" in note
    assert "terminal" in note
    assert "uncertain whether it ran; verify" in note
    assert "secret.txt" not in note
    assert "echo hidden" not in note


def test_rebuild_prior_messages_strips_unfinished_tool_call_from_older_terminal_run() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    aborted_ctx = EventContext(run_id="run:aborted", turn_id="turn:aborted", reply_id="reply:aborted")
    failed_ctx = EventContext(run_id="run:failed", turn_id="turn:failed", reply_id="reply:failed")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(
                **aborted_ctx.event_fields(),
                user_input_chars=10,
                metadata={"user_input": "dangerous task"},
            ),
            ToolCallStartEvent(**aborted_ctx.event_fields(), tool_call_id="call:danger", tool_call_name="terminal"),
            ToolCallDeltaEvent(
                **aborted_ctx.event_fields(),
                tool_call_id="call:danger",
                delta='{"command": "rm -rf ./x"}',
            ),
            ToolCallEndEvent(**aborted_ctx.event_fields(), tool_call_id="call:danger"),
            RequireUserConfirmEvent(
                **aborted_ctx.event_fields(),
                tool_calls=[ToolCallBlock(id="call:danger", name="terminal", input='{"command": "rm -rf ./x"}')],
            ),
            ReplyEndEvent(**aborted_ctx.event_fields()),
            RunEndEvent(**aborted_ctx.event_fields(), status="aborted", stop_reason="aborted"),
            RunStartEvent(
                **failed_ctx.event_fields(),
                user_input_chars=10,
                metadata={"user_input": "failed follow-up"},
            ),
            RunErrorEvent(**failed_ctx.event_fields(), message="provider failed", code="openai_responses_error"),
            RunEndEvent(**failed_ctx.event_fields(), status="failed", stop_reason="model_error"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert [message.role for message in messages] == ["user", "user", "system"]
    assert messages[2].content[0].text == FAILURE_NOTE_TEXT
    assert all(
        not isinstance(block, ToolCallBlock)
        for message in messages
        for block in message.content
    )


def test_rebuild_prior_messages_does_not_inject_aborted_note_when_newer_run_succeeds() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    aborted_ctx = EventContext(run_id="run:aborted", turn_id="turn:aborted", reply_id="reply:aborted")
    done_ctx = EventContext(run_id="run:done", turn_id="turn:done", reply_id="reply:done")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**aborted_ctx.event_fields(), user_input_chars=10, metadata={"user_input": "aborted user"}),
            ReplyEndEvent(**aborted_ctx.event_fields()),
            RunEndEvent(**aborted_ctx.event_fields(), status="aborted", stop_reason="aborted"),
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
        not (message.role == "system" and message.content and message.content[0].text == INTERRUPTED_NOTE_TEXT)
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


def test_rebuild_prior_messages_injects_terminal_completion_note_once_after_previous_run() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    first_ctx = EventContext(run_id="run:first", turn_id="turn:first", reply_id="reply:first")
    second_ctx = EventContext(run_id="run:second", turn_id="turn:second", reply_id="reply:second")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**first_ctx.event_fields(), user_input_chars=10, metadata={"user_input": "run tests"}),
            ReplyEndEvent(**first_ctx.event_fields()),
            RunEndEvent(**first_ctx.event_fields(), status="finished", stop_reason="final"),
            TerminalProcessCompletedEvent(
                **first_ctx.event_fields(),
                process_id="proc_done",
                terminal_session_id="default",
                command="pytest -q",
                status="success",
                exit_code=0,
                cwd="/workspace",
                duration_seconds=1.0,
            ),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert messages[-1].role == "system"
    assert "terminal background task update" in messages[-1].content[0].text
    assert "proc_done" in messages[-1].content[0].text
    assert "if still retained" in messages[-1].content[0].text.lower()

    log.extend(
        [
            RunStartEvent(**second_ctx.event_fields(), user_input_chars=8, metadata={"user_input": "continue"}),
            ReplyEndEvent(**second_ctx.event_fields()),
            RunEndEvent(**second_ctx.event_fields(), status="finished", stop_reason="final"),
        ]
    )
    later_messages = rebuild_prior_messages(log)

    assert all(
        not (message.role == "system" and "terminal background task update" in message.content[0].text)
        for message in later_messages
    )


def test_rebuild_prior_messages_injects_terminal_completion_note_when_completion_happened_during_later_turn() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    first_ctx = EventContext(run_id="run:first", turn_id="turn:first", reply_id="reply:first")
    second_ctx = EventContext(run_id="run:second", turn_id="turn:second", reply_id="reply:second")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**first_ctx.event_fields(), user_input_chars=10, metadata={"user_input": "start server"}),
            ReplyEndEvent(**first_ctx.event_fields()),
            RunEndEvent(**first_ctx.event_fields(), status="finished", stop_reason="final"),
            RunStartEvent(**second_ctx.event_fields(), user_input_chars=8, metadata={"user_input": "do other work"}),
            TerminalProcessCompletedEvent(
                **first_ctx.event_fields(),
                process_id="proc_late",
                terminal_session_id="default",
                command="pytest -q",
                status="error",
                exit_code=1,
                cwd="/workspace",
                duration_seconds=2.0,
            ),
            ReplyEndEvent(**second_ctx.event_fields()),
            RunEndEvent(**second_ctx.event_fields(), status="finished", stop_reason="final"),
        ]
    )

    messages = rebuild_prior_messages(log)

    assert messages[-1].role == "system"
    assert "proc_late" in messages[-1].content[0].text
    assert "exit code 1" in messages[-1].content[0].text


def test_rebuild_prior_messages_terminal_completion_note_is_lifecycle_only_projection() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:first", turn_id="turn:first", reply_id="reply:first")
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "start background"}),
            ReplyEndEvent(**ctx.event_fields()),
            RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
            TerminalProcessCompletedEvent(
                **ctx.event_fields(),
                process_id="proc_projection",
                terminal_session_id="default",
                command="printf SHOULD_NOT_SHOW_COMMAND",
                status="success",
                exit_code=0,
                cwd="/workspace/SHOULD_NOT_SHOW_CWD",
                duration_seconds=1.0,
                output_preview="SHOULD_NOT_SHOW_OUTPUT_PREVIEW",
                output_truncated=True,
                completion_reason="user_tool_kill",
            ),
        ]
    )

    messages = rebuild_prior_messages(log)
    note = messages[-1].content[0].text

    assert "terminal background task update" in note
    assert "proc_projection" in note
    assert "status success" in note
    assert "exit code 0" in note
    assert "lifecycle-only" in note
    assert "terminal_process log" in note
    assert "SHOULD_NOT_SHOW_COMMAND" not in note
    assert "SHOULD_NOT_SHOW_CWD" not in note
    assert "SHOULD_NOT_SHOW_OUTPUT_PREVIEW" not in note
    assert "user_tool_kill" not in note
    assert "full output" in note


def test_rebuild_prior_messages_terminal_completion_note_caps_projected_processes() -> None:
    from pulsara_agent.event_log import InMemoryEventLog

    ctx = EventContext(run_id="run:first", turn_id="turn:first", reply_id="reply:first")
    log = InMemoryEventLog()
    events = [
        RunStartEvent(**ctx.event_fields(), user_input_chars=10, metadata={"user_input": "start background"}),
        ReplyEndEvent(**ctx.event_fields()),
        RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
    ]
    for index in range(4):
        events.append(
            TerminalProcessCompletedEvent(
                **ctx.event_fields(),
                process_id=f"proc_{index}",
                terminal_session_id="default",
                command=f"cmd_{index}",
                status="success",
                exit_code=0,
                cwd="/workspace",
                duration_seconds=1.0,
                output_preview=f"output_{index}",
            )
        )
    log.extend(events)

    messages = rebuild_prior_messages(log)
    note = messages[-1].content[0].text

    assert "proc_0" in note
    assert "proc_1" in note
    assert "proc_2" in note
    assert "proc_3" not in note
    assert "1 more terminal task(s) completed" in note
    assert "cmd_3" not in note
    assert "output_3" not in note


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


def test_host_session_terminal_completion_event_replays_and_injects_one_shot_note(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bg",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "sleep 0.05 && printf BG_DONE", "yield_time_ms": 0}
                        ),
                    }
                ]
            },
            {"text": "started background task"},
            {"text": "continued after completion"},
            {"text": "final check"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        seen_live: list[TerminalProcessCompletedEvent] = []

        class Subscriber:
            async def on_published_event(self, published: RuntimePublishedEvent) -> None:
                if isinstance(published.event, TerminalProcessCompletedEvent):
                    seen_live.append(published.event)

        session.wiring.runtime_wiring.runtime_session.publisher.subscribe(Subscriber())
        await session.run_turn("start background task")
        manager = session.wiring.runtime_wiring.runtime_session.terminal_sessions
        process = manager.list_processes(owner_host_session_id=session.host_session_id)[0]
        manager.wait_process(process.process_id, timeout_seconds=2)
        deadline = asyncio.get_running_loop().time() + 2
        while not seen_live and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        replayed = [event for event in session.replay_events() if isinstance(event, TerminalProcessCompletedEvent)]
        await session.run_turn("continue after background completion")
        second_context = transport.contexts[-1]
        await session.run_turn("check note is gone")
        third_context = transport.contexts[-1]
        return session, replayed, seen_live, second_context, third_context

    session, replayed, seen_live, second_context, third_context = asyncio.run(run())

    assert len(replayed) == 1
    assert len(seen_live) == 1
    assert replayed[0].process_id == seen_live[0].process_id
    assert replayed[0].output_preview == "BG_DONE"
    assert "terminal background task update" in _context_text(second_context)
    assert replayed[0].process_id in _context_text(second_context)
    assert "terminal background task update" not in _context_text(third_context)
    terminal_summary = session.summary()["terminal"]
    assert terminal_summary["finished_process_count"] == 1
    assert terminal_summary["processes"][0]["process_id"] == replayed[0].process_id


def test_host_session_terminal_completion_during_later_turn_appears_in_following_context(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bg",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "sleep 0.25 && printf LATE_DONE", "yield_time_ms": 0}
                        ),
                    }
                ]
            },
            {"text": "started background task"},
            {"text": "slow unrelated turn"},
            {"text": "after late completion"},
        ],
        delay=0.2,
    )
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        await session.run_turn("start background task")
        await session.run_turn("do slow unrelated work")
        second_context = transport.contexts[-1]
        events = [event for event in session.replay_events() if isinstance(event, TerminalProcessCompletedEvent)]
        await session.run_turn("continue after late task")
        third_context = transport.contexts[-1]
        return events, second_context, third_context

    events, second_context, third_context = asyncio.run(run())

    assert len(events) == 1
    assert "terminal background task update" not in _context_text(second_context)
    assert "terminal background task update" in _context_text(third_context)
    assert events[0].process_id in _context_text(third_context)


def test_host_session_terminal_completion_note_is_owner_isolated_across_shared_workspace(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:a-bg",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "sleep 0.05 && printf A_DONE", "yield_time_ms": 0}
                        ),
                    }
                ]
            },
            {"text": "a started"},
            {"text": "b sees no a completion"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        a = await _open_project_session(core, tmp_path, host_session_id="host:a", permission_policy=_trusted_terminal_policy())
        b = await _open_project_session(core, tmp_path, host_session_id="host:b", permission_policy=_trusted_terminal_policy())
        await a.run_turn("start a process")
        manager = a.wiring.runtime_wiring.runtime_session.terminal_sessions
        process = manager.list_processes(owner_host_session_id="host:a")[0]
        manager.wait_process(process.process_id, timeout_seconds=2)
        a_events = [event for event in a.replay_events() if isinstance(event, TerminalProcessCompletedEvent)]
        b_events = [event for event in b.replay_events() if isinstance(event, TerminalProcessCompletedEvent)]
        await b.run_turn("continue b")
        return a_events, b_events, transport.contexts[-1]

    a_events, b_events, b_context = asyncio.run(run())

    assert len(a_events) == 1
    assert b_events == []
    assert "terminal background task update" not in _context_text(b_context)


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


def test_host_session_stores_pending_approval_and_blocks_new_turn_until_resolved(tmp_path, monkeypatch) -> None:
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
            {"text": "approved continuation"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        first = await session.run_turn("attempt dangerous command")
        pending = session.get_pending_approval()
        assert pending is not None
        assert pending.tool_calls[0].id == "call:danger"
        assert session.active_run_id is None
        assert session.suspended_run_id == first.state.run_id
        assert not session._run_lock.locked()
        with pytest.raises(HostSessionPendingApprovalError):
            await session.run_turn("new prompt should not start")
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())

    assert resolved.status.value == "finished"
    assert resolved.final_text == "approved continuation"
    assert session.get_pending_approval() is None
    assert session.suspended_run_id is None
    assert session.active_run_id is None
    assert any(event.run_id == first.state.run_id for event in session.replay_events())


def test_host_session_terminal_access_ask_approval_executes_terminal_snapshot(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ask-terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf PULSARA_ASK_OK"}),
                    }
                ]
            },
            {"text": "approved ask continuation"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_ask_policy())
        first = await session.run_turn("run harmless terminal under ask")
        pending = session.get_pending_approval()
        assert pending is not None
        assert pending.tool_calls[0].name == "terminal"
        assert pending.tool_calls[0].id == "call:ask-terminal"
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == first.state.run_id]
    tool_output = "".join(
        event.delta
        for event in run_events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:ask-terminal"
    )

    assert first.status.value == "waiting_user"
    assert resolved.status.value == "finished"
    assert resolved.final_text == "approved ask continuation"
    assert session.get_pending_approval() is None
    assert "PULSARA_ASK_OK" in tool_output


def test_host_session_on_request_write_approval_executes_file_snapshot(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "approved.txt", "content": "PULSARA_ON_REQUEST_OK\n"}),
                    }
                ]
            },
            {"text": "write approved"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_workspace_on_request_policy())
        first = await session.run_turn("write a file under on_request")
        pending = session.get_pending_approval()
        assert pending is not None
        assert pending.tool_calls[0].name == "write_file"
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())

    assert first.status.value == "waiting_user"
    assert resolved.status.value == "finished"
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "PULSARA_ON_REQUEST_OK\n"
    assert session.get_pending_approval() is None


def test_host_session_on_request_write_deny_leaves_file_absent_and_continues(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "denied.txt", "content": "SHOULD_NOT_EXIST\n"}),
                    }
                ]
            },
            {"text": "write denied"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_workspace_on_request_policy())
        first = await session.run_turn("write a file under on_request")
        pending = session.get_pending_approval()
        assert pending is not None
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(
                    ToolApprovalDecision(tool_call_id=call.id, confirmed=False) for call in pending.tool_calls
                ),
            )
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == first.state.run_id]
    denied_output = "".join(
        event.delta
        for event in run_events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:write"
    )

    assert resolved.status.value == "finished"
    assert resolved.final_text == "write denied"
    assert not (tmp_path / "denied.txt").exists()
    assert "tool call denied by user approval" in denied_output
    assert session.get_pending_approval() is None


def test_host_session_stop_terminal_access_ask_pending_approval_aborts(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ask-terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf SHOULD_NOT_RUN"}),
                    }
                ]
            },
            {"text": "continued after ask stop"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_ask_policy())
        first = await session.run_turn("run harmless terminal under ask")
        assert session.get_pending_approval() is not None
        stopped = await session.stop_current_turn()
        assert stopped is not None
        second = await session.run_turn("please continue")
        return session, first, stopped, second

    session, first, stopped, second = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == first.state.run_id]

    assert stopped.status.value == "aborted"
    assert stopped.stop_reason == "aborted"
    assert second.final_text == "continued after ask stop"
    assert session.get_pending_approval() is None
    assert not any(event.type.name == "TOOL_RESULT_START" for event in run_events)
    assert [event.status for event in run_events if isinstance(event, RunEndEvent)] == ["aborted"]


def test_host_session_stop_on_request_write_pending_approval_aborts_without_file(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "stopped.txt", "content": "SHOULD_NOT_EXIST\n"}),
                    }
                ]
            },
            {"text": "continued after write stop"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_workspace_on_request_policy())
        first = await session.run_turn("write a file under on_request")
        assert session.get_pending_approval() is not None
        stopped = await session.stop_current_turn()
        assert stopped is not None
        second = await session.run_turn("please continue")
        return session, first, stopped, second

    session, first, stopped, second = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == first.state.run_id]

    assert stopped.status.value == "aborted"
    assert stopped.stop_reason == "aborted"
    assert second.final_text == "continued after write stop"
    assert session.get_pending_approval() is None
    assert not (tmp_path / "stopped.txt").exists()
    assert not any(event.type.name == "TOOL_RESULT_START" for event in run_events)
    assert [event.status for event in run_events if isinstance(event, RunEndEvent)] == ["aborted"]


def test_host_session_hardline_under_terminal_ask_denies_without_approval(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:hardline",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf /"}),
                    }
                ]
            },
            {"text": "hardline denied"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_ask_policy())
        result = await session.run_turn("attempt hardline command")
        return session, result

    session, result = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == result.state.run_id]
    denied_output = "".join(
        event.delta
        for event in run_events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:hardline"
    )

    assert result.status.value == "finished"
    assert result.final_text == "hardline denied"
    assert session.get_pending_approval() is None
    assert not any(isinstance(event, RequireUserConfirmEvent) for event in run_events)
    assert "terminal command blocked by hardline permission policy" in denied_output


# --- Preset-driven approval-resume tests (contract main paths) ---------------
# These exercise the four frozen permission presets via preset_to_policy() so
# that any change to a preset's (profile, approval, terminal) triple is caught
# by the approval-resume behavior it implies. See
# contracts/PERMISSION_POLICY_CONTRACT.zh.md §2/§4/§5.


def test_ask_permissions_preset_terminal_suspends_then_executes_on_approve(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ask-terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf PULSARA_PRESET_ASK_OK"}),
                    }
                ]
            },
            {"text": "ask-permissions terminal continuation"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS)
        )
        first = await session.run_turn("run terminal under ask-permissions")
        pending = session.get_pending_approval()
        assert pending is not None
        assert pending.tool_calls[0].name == "terminal"
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == first.state.run_id]
    tool_output = "".join(
        event.delta
        for event in run_events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:ask-terminal"
    )

    assert first.status.value == "waiting_user"
    assert resolved.status.value == "finished"
    assert resolved.final_text == "ask-permissions terminal continuation"
    assert session.get_pending_approval() is None
    assert "PULSARA_PRESET_ASK_OK" in tool_output


def test_ask_permissions_preset_write_suspends_then_executes_on_approve(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ask-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "ask_permissions.txt", "content": "PULSARA_ASK_WRITE_OK\n"}),
                    }
                ]
            },
            {"text": "ask-permissions write continuation"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS)
        )
        first = await session.run_turn("write a file under ask-permissions")
        pending = session.get_pending_approval()
        assert pending is not None
        assert pending.tool_calls[0].name == "write_file"
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        return first, resolved

    first, resolved = asyncio.run(run())

    assert first.status.value == "waiting_user"
    assert resolved.status.value == "finished"
    assert (tmp_path / "ask_permissions.txt").read_text() == "PULSARA_ASK_WRITE_OK\n"


def test_accept_edits_preset_autoallows_write_but_asks_terminal(tmp_path, monkeypatch) -> None:
    # accept-edits = trusted_host / never / ask. The only difference from
    # ask-permissions is that file writes auto-pass while terminal still asks.
    # Writes and the terminal are scripted in separate model rounds because the
    # gate suspends the whole batch on the first non-ALLOW decision; isolating
    # them proves the write auto-executed (file on disk) before any approval.
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:auto-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "accept_edits.txt", "content": "PULSARA_ACCEPT_EDITS_OK\n"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:accept-terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf PULSARA_ACCEPT_TERMINAL_OK"}),
                    }
                ]
            },
            {"text": "accept-edits continuation"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.ACCEPT_EDITS)
        )
        first = await session.run_turn("write a file then run a terminal command")
        # Write auto-executed without suspending; the run only paused on terminal.
        write_exists_at_pause = (tmp_path / "accept_edits.txt").exists()
        pending = session.get_pending_approval()
        assert pending is not None
        assert [call.name for call in pending.tool_calls] == ["terminal"]
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        return session, first, resolved, write_exists_at_pause

    session, first, resolved, write_exists_at_pause = asyncio.run(run())
    terminal_output = "".join(
        event.delta
        for event in session.replay_events()
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:accept-terminal"
    )

    assert first.status.value == "waiting_user"
    assert write_exists_at_pause is True
    assert (tmp_path / "accept_edits.txt").read_text() == "PULSARA_ACCEPT_EDITS_OK\n"
    assert resolved.status.value == "finished"
    assert resolved.final_text == "accept-edits continuation"
    assert "PULSARA_ACCEPT_TERMINAL_OK" in terminal_output


def test_bypass_permissions_preset_runs_without_pending_approval(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bypass-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "bypass.txt", "content": "PULSARA_BYPASS_OK\n"}),
                    },
                    {
                        "id": "call:bypass-terminal",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf PULSARA_BYPASS_TERMINAL_OK"}),
                    },
                ]
            },
            {"text": "bypass continuation"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
        )
        result = await session.run_turn("write and run terminal under bypass")
        return session, result

    session, result = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == result.state.run_id]
    terminal_output = "".join(
        event.delta
        for event in run_events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:bypass-terminal"
    )

    assert result.status.value == "finished"
    assert result.final_text == "bypass continuation"
    assert session.get_pending_approval() is None
    assert not any(isinstance(event, RequireUserConfirmEvent) for event in run_events)
    assert (tmp_path / "bypass.txt").read_text() == "PULSARA_BYPASS_OK\n"
    assert "PULSARA_BYPASS_TERMINAL_OK" in terminal_output


def test_bypass_permissions_preset_still_denies_hardline_terminal(tmp_path, monkeypatch) -> None:
    # Contract §5: bypass means "no approval", NOT "no protection". Hardline
    # terminal commands are denied under every preset, including bypass.
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bypass-hardline",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf /"}),
                    }
                ]
            },
            {"text": "bypass hardline denied"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
        )
        result = await session.run_turn("attempt hardline command under bypass")
        return session, result

    session, result = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == result.state.run_id]
    denied_output = "".join(
        event.delta
        for event in run_events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:bypass-hardline"
    )

    assert result.status.value == "finished"
    assert result.final_text == "bypass hardline denied"
    assert session.get_pending_approval() is None
    assert not any(isinstance(event, RequireUserConfirmEvent) for event in run_events)
    assert "terminal command blocked by hardline permission policy" in denied_output


def test_read_only_preset_denies_write_without_pending_approval(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ro-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "read_only.txt", "content": "SHOULD_NOT_EXIST\n"}),
                    }
                ]
            },
            {"text": "read-only denied write"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.READ_ONLY)
        )
        result = await session.run_turn("attempt write under read-only")
        return session, result

    session, result = asyncio.run(run())
    run_events = [event for event in session.replay_events() if event.run_id == result.state.run_id]
    denied_output = "".join(
        event.delta
        for event in run_events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:ro-write"
    )

    assert result.status.value == "finished"
    assert result.final_text == "read-only denied write"
    assert session.get_pending_approval() is None
    assert not any(isinstance(event, RequireUserConfirmEvent) for event in run_events)
    assert not (tmp_path / "read_only.txt").exists()
    assert "not allowed by permission policy" in denied_output


def test_user_enter_plan_immediately_switches_read_only_and_emits_entry_audit(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:write-in-plan",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "plan_write.txt", "content": "SHOULD_NOT_EXIST\n"}),
                    }
                ]
            },
            {"text": "planned only"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core,
            tmp_path,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        policy = session.enter_plan(reason="user pressed plan")
        assert policy == preset_to_policy(PermissionMode.READ_ONLY)
        assert session.current_permission_mode is PermissionMode.READ_ONLY
        assert session.plan_state.active is True
        assert session.plan_state.pending_entry_audit is True
        result = await session.run_turn("please plan the change")
        return session, result

    session, result = asyncio.run(run())
    events = session.replay_events()
    entered = [event for event in events if isinstance(event, PlanModeEnteredEvent)]
    write_output = "".join(
        event.delta
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:write-in-plan"
    )
    first_context = _context_text(transport.contexts[0])

    assert result.final_text == "planned only"
    assert session.plan_state.active is True
    assert session.plan_state.pending_entry_audit is False
    assert entered[0].source == "user"
    assert entered[0].previous_permission_mode == PermissionMode.BYPASS_PERMISSIONS.value
    assert "Plan workflow is active" in first_context
    assert "please plan the change" in first_context
    assert not (tmp_path / "plan_write.txt").exists()
    assert "not allowed by permission policy" in write_output


def test_agent_enter_plan_tool_switches_read_only_before_next_tool_turn(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:enter-plan",
                        "name": "enter_plan",
                        "arguments": json.dumps({"reason": "need a plan"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:write-after-enter",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "after_enter.txt", "content": "SHOULD_NOT_EXIST\n"}),
                    }
                ]
            },
            {"text": "stayed in plan"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core,
            tmp_path,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        result = await session.run_turn("decide whether to plan")
        return session, result

    session, result = asyncio.run(run())
    events = session.replay_events()
    entered = [event for event in events if isinstance(event, PlanModeEnteredEvent)]
    write_output = "".join(
        event.delta
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:write-after-enter"
    )

    assert result.final_text == "stayed in plan"
    assert session.plan_state.active is True
    assert session.current_permission_mode is PermissionMode.READ_ONLY
    assert entered[0].source == "agent"
    assert entered[0].previous_permission_mode == PermissionMode.BYPASS_PERMISSIONS.value
    assert not (tmp_path / "after_enter.txt").exists()
    assert "not allowed by permission policy" in write_output


def test_plan_question_suspends_and_resolution_continues_same_run(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:question",
                        "name": "ask_plan_question",
                        "arguments": json.dumps(
                            {
                                "question": "Which module should I inspect?",
                                "options": ["runtime", "host"],
                                "allow_free_text": True,
                            }
                        ),
                    }
                ]
            },
            {"text": "question answered"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        session.enter_plan(reason="ask first")
        first = await session.run_turn("plan with a question")
        pending = session.get_pending_interaction()
        assert isinstance(pending, PendingPlanInteraction)
        assert pending.kind == "question"
        assert pending.question == "Which module should I inspect?"
        assert pending.options == ("runtime", "host")
        with pytest.raises(HostSessionPendingInteractionError):
            await session.run_turn("new prompt should not start")
        resolved = await session.resolve_plan_interaction(
            PlanQuestionResolution(
                interaction_id=pending.interaction_id,
                answer_text="runtime",
                selected_option="runtime",
            )
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())
    events = session.replay_events()

    assert first.status.value == "waiting_user"
    assert resolved.status.value == "finished"
    assert resolved.final_text == "question answered"
    assert session.get_pending_interaction() is None
    assert any(isinstance(event, PlanQuestionAskedEvent) for event in events)
    assert any(
        isinstance(event, PlanQuestionAnsweredEvent) and event.answer_text == "runtime"
        for event in events
    )
    assert {event.run_id for event in events if isinstance(event, PlanQuestionAskedEvent | PlanQuestionAnsweredEvent)} == {
        first.state.run_id
    }


def test_exit_plan_approve_restores_pre_plan_permission(tmp_path, monkeypatch) -> None:
    expected_plan_text = "1. Edit file. 2. Run tests."
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:exit",
                        "name": "exit_plan",
                        "arguments": json.dumps({"plan": expected_plan_text, "summary": "edit and test"}),
                    }
                ]
            },
            {"text": "approved execution can begin"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core,
            tmp_path,
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        session.enter_plan(reason="make a plan")
        first = await session.run_turn("submit plan")
        pending = session.get_pending_interaction()
        assert isinstance(pending, PendingPlanInteraction)
        assert pending.kind == "exit"
        resolved = await session.resolve_plan_interaction(
            PlanExitResolution(
                interaction_id=pending.interaction_id,
                decision="approve",
                user_feedback="looks good",
            )
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())
    events = session.replay_events()

    assert first.status.value == "waiting_user"
    assert resolved.final_text == "approved execution can begin"
    assert session.plan_state.active is False
    assert session.current_permission_mode is PermissionMode.BYPASS_PERMISSIONS
    assert any(isinstance(event, PlanExitRequestedEvent) for event in events)
    assert any(isinstance(event, PlanExitResolvedEvent) and event.decision == "approve" for event in events)
    exited = [event for event in events if isinstance(event, PlanModeExitedEvent)]
    assert exited[0].source == "approved_exit_plan"
    assert exited[0].restored_permission_mode == PermissionMode.BYPASS_PERMISSIONS.value
    assert exited[0].accepted_plan_artifact_id
    assert session.plan_state.latest_accepted_plan_artifact_id == exited[0].accepted_plan_artifact_id
    assert session.wiring.runtime_wiring.runtime_session.archive.get_text(
        exited[0].accepted_plan_artifact_id
    ) == expected_plan_text
    post_approval_context = _context_text(transport.contexts[1])
    assert "Plan workflow is active" not in post_approval_context
    assert "You are still in Plan workflow" not in post_approval_context


def test_exit_plan_revise_keeps_plan_active_and_read_only(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:exit",
                        "name": "exit_plan",
                        "arguments": json.dumps({"plan": "draft", "summary": "draft"}),
                    }
                ]
            },
            {"text": "revising plan"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        session.enter_plan(reason="revise path")
        await session.run_turn("submit plan")
        pending = session.get_pending_interaction()
        assert isinstance(pending, PendingPlanInteraction)
        result = await session.resolve_plan_interaction(
            PlanExitResolution(
                interaction_id=pending.interaction_id,
                decision="revise",
                user_feedback="add tests",
            )
        )
        return session, result

    session, result = asyncio.run(run())

    assert result.final_text == "revising plan"
    assert session.plan_state.active is True
    assert session.current_permission_mode is PermissionMode.READ_ONLY
    assert not any(isinstance(event, PlanModeExitedEvent) for event in session.replay_events())


def test_workflow_tool_batch_barrier_does_not_execute_sibling_write(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:question",
                        "name": "ask_plan_question",
                        "arguments": json.dumps({"question": "Proceed?"}),
                    },
                    {
                        "id": "call:sibling-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "sibling.txt", "content": "SHOULD_NOT_EXIST\n"}),
                    },
                ]
            }
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        session.enter_plan(reason="barrier")
        result = await session.run_turn("ask and write in one batch")
        return session, result

    session, result = asyncio.run(run())
    events = session.replay_events()
    sibling_output = "".join(
        event.delta
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent) and event.tool_call_id == "call:sibling-write"
    )

    assert result.status.value == "waiting_user"
    assert isinstance(session.get_pending_interaction(), PendingPlanInteraction)
    assert not (tmp_path / "sibling.txt").exists()
    assert "not executed because a plan workflow control tool" in sibling_output


def test_stop_pending_plan_interaction_keeps_plan_active_read_only(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:question",
                        "name": "ask_plan_question",
                        "arguments": json.dumps({"question": "Scope?"}),
                    }
                ]
            },
            {"text": "still planning"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        session.enter_plan(reason="stop question")
        first = await session.run_turn("ask")
        assert isinstance(session.get_pending_interaction(), PendingPlanInteraction)
        stopped = await session.stop_current_turn()
        assert stopped is not None
        second = await session.run_turn("continue planning")
        return session, first, stopped, second

    session, first, stopped, second = asyncio.run(run())

    assert first.status.value == "waiting_user"
    assert stopped.status.value == "aborted"
    assert second.final_text == "still planning"
    assert session.plan_state.active is True
    assert session.current_permission_mode is PermissionMode.READ_ONLY
    assert session.get_pending_interaction() is None
    assert not any(isinstance(event, PlanModeExitedEvent) for event in session.replay_events())


def test_plan_question_budget_exhaustion_fails_run_with_plan_specific_error(tmp_path, monkeypatch) -> None:
    # §9.6: plan HITL has its own per-run budget. A second ask_plan_question
    # after the budget of 1 trips a plan-specific failure, not an ordinary
    # tool-error budget failure.
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:q1",
                        "name": "ask_plan_question",
                        "arguments": json.dumps({"question": "First?"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:q2",
                        "name": "ask_plan_question",
                        "arguments": json.dumps({"question": "Second?"}),
                    }
                ]
            },
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        session.wiring.agent_runtime.budget = LoopBudget(max_plan_interactions_per_run=1)
        session.enter_plan(reason="budget")
        first = await session.run_turn("plan with questions")
        pending = session.get_pending_interaction()
        assert isinstance(pending, PendingPlanInteraction)
        # Resolving the first question continues the same run, where the model
        # immediately asks again and trips the interaction budget.
        resolved = await session.resolve_plan_interaction(
            PlanQuestionResolution(interaction_id=pending.interaction_id, answer_text="ok")
        )
        return session, first, resolved

    session, first, resolved = asyncio.run(run())
    events = session.replay_events()

    assert first.status.value == "waiting_user"
    assert resolved.status.value == "failed"
    assert resolved.stop_reason == "plan_interaction_budget"
    assert session.get_pending_interaction() is None
    # The failure is plan-specific, not an ordinary tool-error budget failure.
    assert any(
        isinstance(event, RunErrorEvent) and event.code == "plan_interaction_budget_exceeded"
        for event in events
    )
    # Plan stays active / read-only; budget exhaustion is not an approved exit.
    assert session.plan_state.active is True
    assert session.current_permission_mode is PermissionMode.READ_ONLY
    assert not any(isinstance(event, PlanModeExitedEvent) for event in events)


def test_plan_exit_revision_budget_exhaustion_fails_run(tmp_path, monkeypatch) -> None:
    # §9.6: exit_plan revisions have a dedicated budget. With a limit of 1, the
    # second revise trips the budget rather than looping forever.
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:exit1",
                        "name": "exit_plan",
                        "arguments": json.dumps({"plan": "draft one", "summary": "v1"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:exit2",
                        "name": "exit_plan",
                        "arguments": json.dumps({"plan": "draft two", "summary": "v2"}),
                    }
                ]
            },
            {"text": "should not reach here"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        session.wiring.agent_runtime.budget = LoopBudget(
            max_plan_interactions_per_run=8,
            max_plan_exit_revisions_per_run=1,
        )
        session.enter_plan(reason="budget")
        first = await session.run_turn("plan then exit")
        first_pending = session.get_pending_interaction()
        assert isinstance(first_pending, PendingPlanInteraction)
        assert first_pending.kind == "exit"
        # First revise (revisions=1, within budget) -> continue, model exits again.
        second = await session.resolve_plan_interaction(
            PlanExitResolution(
                interaction_id=first_pending.interaction_id,
                decision="revise",
                user_feedback="tighten scope",
            )
        )
        second_pending = session.get_pending_interaction()
        assert isinstance(second_pending, PendingPlanInteraction)
        # Second revise (revisions=2, over budget of 1) -> plan-specific failure.
        third = await session.resolve_plan_interaction(
            PlanExitResolution(
                interaction_id=second_pending.interaction_id,
                decision="revise",
                user_feedback="again",
            )
        )
        return session, first, second, third

    session, first, second, third = asyncio.run(run())
    events = session.replay_events()

    assert first.status.value == "waiting_user"
    assert second.status.value == "waiting_user"
    assert third.status.value == "failed"
    assert third.stop_reason == "plan_interaction_budget"
    assert any(
        isinstance(event, RunErrorEvent) and event.code == "plan_interaction_budget_exceeded"
        for event in events
    )
    # Revise never restores permission; plan stays active / read-only.
    assert session.plan_state.active is True
    assert session.current_permission_mode is PermissionMode.READ_ONLY
    assert not any(isinstance(event, PlanModeExitedEvent) for event in events)


def test_host_session_suspension_releases_run_lock_before_approval_resolution(tmp_path, monkeypatch) -> None:
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
            {"text": "resolved without deadlock"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        await session.run_turn("attempt dangerous command")
        pending = session.get_pending_approval()
        assert pending is not None
        assert not session._run_lock.locked()
        result = await asyncio.wait_for(
            session.resolve_approval(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(
                        ToolApprovalDecision(tool_call_id=call.id, confirmed=False) for call in pending.tool_calls
                    ),
                )
            ),
            timeout=1,
        )
        return session, result

    session, result = asyncio.run(run())

    assert result.final_text == "resolved without deadlock"
    assert session.get_pending_approval() is None
    assert not session._run_lock.locked()


def test_host_session_stop_pending_approval_aborts_without_tool_execution(tmp_path, monkeypatch) -> None:
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
            {"text": "continued after stop"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        first = await session.run_turn("attempt dangerous command")
        pending = session.get_pending_approval()
        assert pending is not None
        stopped = await session.stop_current_turn()
        assert stopped is not None
        second = await session.run_turn("please continue")
        return session, first, stopped, second

    session, first, stopped, second = asyncio.run(run())
    events = session.replay_events()
    run_events = [event for event in events if event.run_id == first.state.run_id]

    assert stopped.status.value == "aborted"
    assert stopped.stop_reason == "aborted"
    assert second.final_text == "continued after stop"
    assert session.get_pending_approval() is None
    assert session.suspended_run_id is None
    assert not any(event.type.name == "TOOL_RESULT_START" for event in run_events)
    assert [event.status for event in run_events if isinstance(event, RunEndEvent)] == ["aborted"]
    assert any(
        message.role.value == "system" and INTERRUPTED_NOTE_TEXT in "\n".join(message.content)
        for message in transport.contexts[-1].messages
    )


def test_host_core_stop_current_turn_delegates_to_session(tmp_path, monkeypatch) -> None:
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
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        await session.run_turn("attempt dangerous command")
        return await core.stop_current_turn(session.host_session_id)

    stopped = asyncio.run(run())

    assert stopped is not None
    assert stopped.status.value == "aborted"


def test_host_session_stop_active_run_turn_aborts_and_releases_lock(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "continued"}], delay=0.2)
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path)
        run_task = asyncio.create_task(session.run_turn("slow user request"))
        await asyncio.sleep(0.05)
        stopped = await session.stop_current_turn()
        result = await run_task
        assert not session._run_lock.locked()
        second = await session.run_turn("continue after stop")
        return session, stopped, result, second

    session, stopped, result, second = asyncio.run(run())
    first_run_id = result.state.run_id
    first_events = [event for event in session.replay_events() if event.run_id == first_run_id]

    assert stopped is not None
    assert stopped.status.value == "aborted"
    assert result.status.value == "aborted"
    assert result.state.stop_request is None
    assert result.state.abort_kind is not None
    assert result.state.abort_kind.value == "user_stop"
    assert second.final_text == "continued"
    assert session.active_run_id is None
    assert session.stopping_run_id is None
    assert [event.status for event in first_events if isinstance(event, RunEndEvent)] == ["aborted"]
    assert any(
        message.role.value == "system" and INTERRUPTED_NOTE_TEXT in "\n".join(message.content)
        for message in transport.contexts[-1].messages
    )


def test_host_session_stop_active_run_publishes_aborted_event_to_live_subscriber(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([], delay=0.2)
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)
    delivered: list[AgentEvent] = []

    class Subscriber:
        async def on_published_event(self, published: RuntimePublishedEvent) -> None:
            delivered.append(published.event)

    async def run():
        session = await _open_project_session(core, tmp_path)
        session.wiring.runtime_wiring.runtime_session.publisher.subscribe(Subscriber())
        run_task = asyncio.create_task(session.run_turn("slow user request"))
        await asyncio.sleep(0.05)
        stopped = await session.stop_current_turn()
        result = await run_task
        return result, stopped

    result, stopped = asyncio.run(run())

    assert stopped is not None
    assert stopped.status.value == "aborted"
    assert result.status.value == "aborted"
    assert any(
        isinstance(event, RunEndEvent)
        and event.run_id == result.state.run_id
        and event.status == "aborted"
        and event.stop_reason == "aborted"
        for event in delivered
    )


def test_host_session_stop_remains_busy_when_transport_swallows_cancellation(tmp_path, monkeypatch) -> None:
    class CancellationSwallowingTransport:
        api = "scripted"

        def __init__(self) -> None:
            self.contexts: list[LLMContext] = []
            self.started: asyncio.Event | None = None
            self.release: asyncio.Event | None = None

        async def stream(
            self,
            *,
            model: ModelProfile,
            context: LLMContext,
            event_context: EventContext,
            options: LLMOptions | None = None,
        ) -> AsyncIterator[AgentEvent]:
            self.contexts.append(context)
            assert self.started is not None
            assert self.release is not None
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await self.release.wait()
            yield ModelCallStartEvent(
                **event_context.event_fields(),
                model_name=model.id,
                model_role=model.role.value,
                provider=model.provider,
            )
            yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:late")
            yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:late", delta="late text")
            yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:late")
            yield ModelCallEndEvent(**event_context.event_fields())

    transport = CancellationSwallowingTransport()
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        transport.started = asyncio.Event()
        transport.release = asyncio.Event()
        session = await _open_project_session(core, tmp_path)
        run_task = asyncio.create_task(session.run_turn("slow user request"))
        await transport.started.wait()
        stopped = await session.stop_current_turn(timeout=0.01)
        assert stopped is None
        with pytest.raises(HostSessionBusyError):
            await session.run_turn("must not start while stopping")
        transport.release.set()
        result = await run_task
        return session, result

    session, result = asyncio.run(run())
    first_events = [event for event in session.replay_events() if event.run_id == result.state.run_id]

    assert result.status.value == "aborted"
    assert session.stopping_run_id is None
    assert [event.status for event in first_events if isinstance(event, RunEndEvent)] == ["aborted"]


def test_host_session_resume_can_suspend_again_with_new_pending_approval(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:first",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build-a"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:second",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build-b"}),
                    }
                ]
            },
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        first = await session.run_turn("attempt dangerous command")
        first_pending = session.get_pending_approval()
        assert first_pending is not None
        resumed = await session.resolve_approval(
            ApprovalResolution(
                approval_id=first_pending.approval_id,
                decisions=tuple(
                    ToolApprovalDecision(tool_call_id=call.id, confirmed=False) for call in first_pending.tool_calls
                ),
            )
        )
        second_pending = session.get_pending_approval()
        return first, resumed, first_pending, second_pending, session

    first, resumed, first_pending, second_pending, session = asyncio.run(run())

    assert resumed.status.value == "waiting_user"
    assert second_pending is not None
    assert second_pending.approval_id != first_pending.approval_id
    assert second_pending.run_id == first_pending.run_id == first.state.run_id
    assert second_pending.tool_calls[0].id == "call:second"
    assert session.suspended_run_id == first.state.run_id
    assert session.active_run_id is None


def test_host_session_close_invalidates_pending_approval(tmp_path, monkeypatch) -> None:
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
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        await session.run_turn("attempt dangerous command")
        pending = session.get_pending_approval()
        assert pending is not None
        session.close()
        assert session.get_pending_approval() is None
        assert session.suspended_run_id is None
        with pytest.raises(RuntimeError, match="closed"):
            await session.resolve_approval(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(
                        ToolApprovalDecision(tool_call_id=call.id, confirmed=False) for call in pending.tool_calls
                    ),
                )
            )
        return session

    session = asyncio.run(run())

    assert session.closed


def test_host_session_stream_turn_captures_pending_state_and_resolves_deny(tmp_path, monkeypatch) -> None:
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
            {"text": "denied continuation"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        streamed = [event async for event in session.stream_turn("attempt dangerous command")]
        pending = session.get_pending_approval()
        assert pending is not None
        assert session.suspended_run_id is not None
        assert session.active_run_id is None
        events = [
            event
            async for event in session.stream_approval_resolution(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(
                        ToolApprovalDecision(tool_call_id=call.id, confirmed=False) for call in pending.tool_calls
                    ),
                )
            )
        ]
        return session, streamed, events

    session, streamed, events = asyncio.run(run())

    assert any(event.type.name == "REQUIRE_USER_CONFIRM" for event in streamed)
    assert any(event.type.name == "USER_CONFIRM_RESULT" for event in events)
    assert any(event.type.name == "TOOL_RESULT_END" for event in events)
    assert session.get_pending_approval() is None


def test_host_session_stream_turn_captures_suspended_run_id_before_clearing_active_run(tmp_path, monkeypatch) -> None:
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
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(core, tmp_path, permission_policy=_trusted_terminal_policy())
        streamed = [event async for event in session.stream_turn("attempt dangerous command")]
        pending = session.get_pending_approval()
        confirm = next(event for event in streamed if isinstance(event, RequireUserConfirmEvent))
        return session, pending, confirm

    session, pending, confirm = asyncio.run(run())

    assert pending is not None
    assert session.active_run_id is None
    assert session.suspended_run_id == pending.run_id == confirm.run_id
    assert pending.reply_id == confirm.reply_id


def test_host_core_approval_facade_resolves_pending_request(tmp_path, monkeypatch) -> None:
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
            {"text": "core approved"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core,
            tmp_path,
            host_session_id="host:approval-facade",
            permission_policy=_trusted_terminal_policy(),
        )
        await session.run_turn("attempt dangerous command")
        pending = await core.get_pending_approval(session.host_session_id)
        assert pending is not None
        result = await core.resolve_approval(
            session.host_session_id,
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            ),
        )
        return result, await core.get_pending_approval(session.host_session_id)

    result, pending_after = asyncio.run(run())

    assert result.final_text == "core approved"
    assert pending_after is None


def test_confirm_result_rules_are_inert_in_message_replay() -> None:
    ctx = EventContext(run_id="run:confirm", turn_id="turn:confirm", reply_id="reply:confirm")
    tool_call = ToolCallBlock(
        id="call:danger",
        name="terminal",
        input='{"command":"rm -rf build"}',
        state=ToolCallState.PENDING,
    )
    message = AssistantMsg(id=ctx.reply_id, name="assistant", content=[tool_call])
    reducer = MessageReducer(message)

    reducer.append(
        RequireUserConfirmEvent(
            **ctx.event_fields(),
            tool_calls=[
                ToolCallBlock(
                    id="call:danger",
                    name="terminal",
                    input='{"command":"rm -rf build"}',
                    state=ToolCallState.ASKING,
                    suggested_rules=[{"reason": "dangerous_terminal_command"}],
                )
            ],
        )
    )
    reducer.append(
        UserConfirmResultEvent(
            **ctx.event_fields(),
            confirm_results=[
                ConfirmResult(
                    confirmed=True,
                    tool_call=tool_call,
                    rules=[{"reason": "user_approved_once"}],
                )
            ],
        )
    )

    replayed_call = message.content[0]
    assert isinstance(replayed_call, ToolCallBlock)
    assert replayed_call.state is ToolCallState.ALLOWED
    assert replayed_call.suggested_rules == [{"reason": "dangerous_terminal_command"}]
    assert "rules" not in replayed_call.model_dump(mode="json")


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
        supervisor_summary = (await core.list_workspace_supervisors())[0]
        supervisor_process_ids = {process["process_id"] for process in supervisor_summary["processes"]}
        with pytest.raises(KeyError):
            manager.poll_process(a_proc, owner_host_session_id="host:b")
        await core.close_session("host:a")
        a_status = manager.poll_process(a_proc).status
        b_status = manager.poll_process(b_proc).status
        await core.close_workspace(a.workspace.workspace_key)
        b_status_after_workspace_close = manager.poll_process(b_proc).status
        remaining_sessions = await core.list_sessions()
        return a_status, b_status, b_status_after_workspace_close, remaining_sessions, supervisor_process_ids, {a_proc, b_proc}

    (
        a_status,
        b_status,
        b_status_after_workspace_close,
        remaining_sessions,
        supervisor_process_ids,
        expected_process_ids,
    ) = asyncio.run(run())

    assert supervisor_process_ids == expected_process_ids
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


# --- Step 4: conversational permission-mode switching -----------------------
# Mode is mutable session state, switchable at a turn boundary by the user/host
# only. Tools stay fully visible across modes; the gate denies at call time.


def test_host_session_switch_mode_changes_gate_behavior_next_turn(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ro-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "switch.txt", "content": "SHOULD_NOT_EXIST\n"}),
                    }
                ]
            },
            {"text": "read-only denied"},
            {
                "tool_calls": [
                    {
                        "id": "call:bypass-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "switch.txt", "content": "PULSARA_SWITCH_OK\n"}),
                    }
                ]
            },
            {"text": "bypass wrote"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.READ_ONLY)
        )
        first = await session.run_turn("try to write under read-only")
        denied_absent = not (tmp_path / "switch.txt").exists()
        # User switches mode mid-conversation (turn boundary).
        policy = session.set_permission_mode("bypass-permissions")
        second = await session.run_turn("now write under bypass")
        return session, first, second, denied_absent, policy

    session, first, second, denied_absent, policy = asyncio.run(run())

    assert first.status.value == "finished"
    assert denied_absent  # write blocked while read-only
    assert policy.terminal.value == "allow"
    assert session.current_permission_mode is PermissionMode.BYPASS_PERMISSIONS
    assert second.status.value == "finished"
    assert (tmp_path / "switch.txt").read_text() == "PULSARA_SWITCH_OK\n"


def test_host_session_switch_mode_rejected_while_pending_approval(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ask-write",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "pending.txt", "content": "x\n"}),
                    }
                ]
            },
            {"text": "after approve"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS)
        )
        await session.run_turn("write under ask-permissions")
        assert session.get_pending_approval() is not None
        # Switching while an approval is pending must be rejected.
        rejected = False
        try:
            session.set_permission_mode("bypass-permissions")
        except HostSessionPendingApprovalError:
            rejected = True
        return session, rejected

    session, rejected = asyncio.run(run())
    assert rejected
    # Mode unchanged after the rejected switch.
    assert session.current_permission_mode is PermissionMode.ASK_PERMISSIONS


def test_host_session_switch_mode_preserves_live_terminal_process(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:yield",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "sleep 30", "yield_time_ms": 50}
                        ),
                    }
                ]
            },
            {"text": "yielded a process"},
        ]
    )
    core = _core(monkeypatch, transport, use_workspace_supervisor=False)

    async def run():
        session = await _open_project_session(
            core, tmp_path, permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
        )
        try:
            await session.run_turn("start a long process")
            live_before = session.has_live_processes
            # Switch mode; the live terminal process must survive (zero rebuild).
            session.set_permission_mode("read-only")
            live_after = session.has_live_processes
            return live_before, live_after
        finally:
            await core.close_session(session.host_session_id)

    live_before, live_after = asyncio.run(run())
    assert live_before is True
    assert live_after is True  # process survived the mode switch
