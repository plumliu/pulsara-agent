"""Claude Code-like main loop built on RuntimeSession."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Literal

from pulsara_agent.event import (
    AgentEvent,
    CustomEvent,
    EventContext,
    ExceedMaxItersEvent,
    InMemoryEventLog,
    ProjectionFailedEvent,
    ProjectionReadyEvent,
    ProjectionRequestedEvent,
    RequireUserConfirmEvent,
    RunErrorEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory.provenance import runtime_event_span_from_events
from pulsara_agent.message import (
    AssistantMsg,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    Usage,
    UserMsg,
)
from pulsara_agent.message.assembler import completed_tool_result_from_events
from pulsara_agent.runtime.context import build_llm_context, msg_to_llm_messages
from pulsara_agent.runtime.hooks import MemoryHooks, NoopMemoryHooks, ToolResultPersistenceHook
from pulsara_agent.runtime.permission import (
    AllowAllPermissionGate,
    PermissionDecisionKind,
    PermissionGate,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.state import LoopBudget, LoopState, LoopStatus, LoopTransition
from pulsara_agent.tools import ToolCall, ToolExecutor


StopReason = Literal[
    "final",
    "max_turns",
    "model_error",
    "tool_error_budget",
    "memory_hook_error",
    "waiting_user",
    "aborted",
]


@dataclass(slots=True)
class AgentRunResult:
    status: LoopStatus
    stop_reason: StopReason | None
    state: LoopState
    messages: list[Msg]
    final_text: str
    error_message: str | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        llm_runtime: LLMRuntime,
        memory_hooks: MemoryHooks | None = None,
        tool_result_persistence_hook: ToolResultPersistenceHook | None = None,
        permission_gate: PermissionGate | None = None,
        model_role: ModelRole = ModelRole.PRO,
        options: LLMOptions | None = None,
        budget: LoopBudget | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.runtime_session = runtime_session
        self.llm_runtime = llm_runtime
        self.memory_hooks = memory_hooks or NoopMemoryHooks()
        self.tool_result_persistence_hook = tool_result_persistence_hook
        self.permission_gate = permission_gate or AllowAllPermissionGate()
        self.model_role = model_role
        self.options = options
        self.budget = budget or LoopBudget()
        self.system_prompt = system_prompt
        self.tool_executor = runtime_session.create_tool_executor()
        self._last_result: AgentRunResult | None = None

    async def run_task(self, user_input: str) -> AgentRunResult:
        async for _event in self.stream_task(user_input):
            pass
        assert self._last_result is not None
        return self._last_result

    async def stream_task(self, user_input: str) -> AsyncIterator[AgentEvent]:
        state = LoopState(session_id=self.runtime_session.runtime_session_id, budget=self.budget)
        state.messages.append(UserMsg(name="user", content=user_input))
        yield self._append_lifecycle_event(state, "session_started", {"user_input_chars": len(user_input)})
        ok, _result, error_event = await self._run_memory_hook(
            state,
            "on_session_start",
            lambda: self.memory_hooks.on_session_start(state, user_input),
        )
        if not ok:
            assert error_event is not None
            yield error_event
            async for event in self._finalize_run(state, run_session_end_hook=False):
                yield event
            return

        while state.status is LoopStatus.RUNNING:
            if state.turn_index >= self.budget.max_turns:
                event = self.runtime_session.event_log.append(
                    ExceedMaxItersEvent(
                        **self._event_context(state).event_fields(),
                        name="agent_runtime",
                        max_iters=self.budget.max_turns,
                    )
                )
                state.status = LoopStatus.FAILED
                state.stop_reason = "max_turns"
                state.transition(LoopTransition.EXCEED_MAX_ITERS)
                yield event
                break

            async for event in self._project_memory(state):
                yield event

            context = build_llm_context(
                state=state,
                registry=self.tool_executor.registry,
                system_prompt=self.system_prompt,
                budget=self.budget,
            )

            reply_had_run_error = False
            try:
                async for event in self.llm_runtime.stream(
                    role=self.model_role,
                    context=context,
                    event_context=self._event_context(state),
                    options=self.options,
                ):
                    stored = self.runtime_session.event_log.append(event)
                    if isinstance(stored, RunErrorEvent):
                        reply_had_run_error = True
                    yield stored
            except Exception as exc:
                event = self.runtime_session.event_log.append(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=f"{type(exc).__name__}: {exc}",
                        code="model_stream_error",
                    )
                )
                reply_had_run_error = True
                yield event

            if reply_had_run_error:
                if not self._recover_or_fail_model(state):
                    break
                state.begin_next_turn()
                continue

            assistant = self.runtime_session.event_log.replay(state.reply_id)
            state.messages.append(assistant)
            _accumulate_usage(state, assistant)
            ok, _result, error_event = await self._run_memory_hook(
                state,
                "after_model_reply",
                lambda: self.memory_hooks.after_model_reply(state, assistant),
            )
            if not ok:
                assert error_event is not None
                yield error_event
                break

            tool_blocks = _tool_call_blocks(assistant)
            if not tool_blocks:
                state.status = LoopStatus.FINISHED
                state.stop_reason = "final"
                state.transition(LoopTransition.FINISH)
                break

            state.pending_tool_calls = tool_blocks
            state.transition(LoopTransition.CONTINUE_AFTER_MODEL)
            async for event in self._execute_tool_blocks(state, tool_blocks):
                yield event
            if state.status is not LoopStatus.RUNNING:
                break

            tool_error_count = sum(1 for result in state.tool_results if result.state is not ToolResultState.SUCCESS)
            if tool_error_count:
                state.consecutive_tool_failures += tool_error_count
                state.recovery_mode = True
                if state.consecutive_tool_failures > self.budget.max_consecutive_tool_failures:
                    state.status = LoopStatus.FAILED
                    state.stop_reason = "tool_error_budget"
                    state.error_message = "tool error budget exceeded"
                    state.transition(LoopTransition.FAIL)
                    break
            else:
                state.consecutive_tool_failures = 0
                state.recovery_mode = False

            if self.tool_result_persistence_hook is not None:
                event = await self._run_tool_result_persistence_hook(state)
                if event is not None:
                    yield event
            ok, _result, error_event = await self._run_memory_hook(
                state,
                "after_tool_results",
                lambda: self.memory_hooks.after_tool_results(state, state.tool_results),
            )
            if not ok:
                assert error_event is not None
                yield error_event
                break
            ok, should_compact, error_event = await self._run_memory_hook(
                state,
                "should_compact",
                lambda: self.memory_hooks.should_compact(state),
            )
            if not ok:
                assert error_event is not None
                yield error_event
                break
            if should_compact:
                state.compacted = True
                yield self._append_lifecycle_event(state, "compaction_requested", {})
            state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
            state.begin_next_turn()

        async for event in self._finalize_run(state):
            yield event

    async def _finalize_run(
        self,
        state: LoopState,
        *,
        run_session_end_hook: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        if run_session_end_hook:
            ok, _result, error_event = await self._run_memory_hook(
                state,
                "on_session_end",
                lambda: self.memory_hooks.on_session_end(state),
            )
            if not ok:
                assert error_event is not None
                yield error_event
        yield self._append_lifecycle_event(
            state,
            "session_completed",
            {"status": state.status.value, "stop_reason": state.stop_reason},
        )
        self._last_result = AgentRunResult(
            status=state.status,
            stop_reason=state.stop_reason,
            state=state,
            messages=list(state.messages),
            final_text=_final_text(state.messages),
            error_message=state.error_message,
        )

    async def _run_memory_hook(self, state: LoopState, hook_name: str, call):
        try:
            return True, await call(), None
        except Exception as exc:
            event = self._mark_memory_hook_failed(state, hook_name, exc)
            return False, None, event

    async def _run_tool_result_persistence_hook(self, state: LoopState) -> AgentEvent | None:
        assert self.tool_result_persistence_hook is not None
        try:
            await self.tool_result_persistence_hook.after_tool_results(state, state.tool_results)
            return None
        except Exception as exc:
            return self.runtime_session.event_log.append(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="tool_result_persistence_failed",
                    value={
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )

    def _mark_memory_hook_failed(self, state: LoopState, hook_name: str, exc: Exception) -> AgentEvent:
        message = f"memory hook {hook_name} failed: {type(exc).__name__}: {exc}"
        state.status = LoopStatus.FAILED
        state.stop_reason = "memory_hook_error"
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return self.runtime_session.event_log.append(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="memory_hook_error",
                metadata={"hook": hook_name},
            )
        )

    def _mark_tool_budget_exceeded(self, state: LoopState, *, attempted_count: int) -> AgentEvent:
        message = (
            "tool call budget exceeded before execution: "
            f"current={state.tool_call_count}, attempted={attempted_count}, max={self.budget.max_tool_calls}"
        )
        state.status = LoopStatus.FAILED
        state.stop_reason = "tool_error_budget"
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return self.runtime_session.event_log.append(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="tool_budget_exceeded",
                metadata={
                    "current_tool_call_count": state.tool_call_count,
                    "attempted_tool_call_count": attempted_count,
                    "max_tool_calls": self.budget.max_tool_calls,
                },
            )
        )

    async def _project_memory(self, state: LoopState) -> AsyncIterator[AgentEvent]:
        projection_id = f"projection:{state.turn_id}"
        context = self._event_context(state)
        yield self.runtime_session.event_log.append(
            ProjectionRequestedEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
            )
        )
        try:
            projection = await self.memory_hooks.project(
                state,
                token_budget=self.budget.projection_token_budget,
            )
        except Exception as exc:
            state.memory_projection = None
            yield self.runtime_session.event_log.append(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        state.memory_projection = projection
        yield self.runtime_session.event_log.append(
            ProjectionReadyEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
                included_memory_ids=_projection_ids(projection),
                summary=_projection_summary(projection),
            )
        )

    async def _execute_tool_blocks(
        self,
        state: LoopState,
        tool_blocks: list[ToolCallBlock],
    ) -> AsyncIterator[AgentEvent]:
        parsed_calls: list[ToolCall] = []
        for block in tool_blocks:
            try:
                parsed_calls.append(_parse_tool_call(block))
            except ValueError as exc:
                stored_events = emit_tool_result_error(
                    self.runtime_session.event_log,
                    self._event_context(state),
                    tool_call_id=block.id,
                    tool_call_name=block.name,
                    message=str(exc),
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, block.id)
                _remember_tool_result_event_span(state, stored_events, block.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(
                        role="tool_result",
                        name=block.name,
                        id=f"tool-result-message:{block.id}",
                        content=[result_block],
                    )
                )

        if not parsed_calls:
            return

        decision = await self.permission_gate.evaluate(parsed_calls)
        if decision.kind is PermissionDecisionKind.WAIT_FOR_USER:
            blocks = [
                ToolCallBlock(
                    id=call.id,
                    name=call.name,
                    input=json.dumps(call.arguments),
                    state=ToolCallState.ASKING,
                    suggested_rules=decision.suggested_rules,
                )
                for call in parsed_calls
            ]
            event = self.runtime_session.event_log.append(
                RequireUserConfirmEvent(**self._event_context(state).event_fields(), tool_calls=blocks)
            )
            state.status = LoopStatus.WAITING_USER
            state.stop_reason = "waiting_user"
            state.transition(LoopTransition.WAIT_FOR_USER)
            yield event
            return
        if decision.kind is PermissionDecisionKind.DENY:
            for call in parsed_calls:
                stored_events = emit_tool_result_error(
                    self.runtime_session.event_log,
                    self._event_context(state),
                    tool_call_id=call.id,
                    tool_call_name=call.name,
                    message=decision.reason or "tool call denied by permission gate",
                    state=ToolResultState.DENIED,
                )
                for event in stored_events:
                    yield event
                result_block = _tool_result_from_event_slice(stored_events, call.id)
                _remember_tool_result_event_span(state, stored_events, call.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(
                        role="tool_result",
                        name=call.name,
                        id=f"tool-result-message:{call.id}",
                        content=[result_block],
                    )
                )
            return

        for batch in _tool_batches(parsed_calls, self.tool_executor):
            if state.tool_call_count + len(batch) > self.budget.max_tool_calls:
                yield self._mark_tool_budget_exceeded(state, attempted_count=len(batch))
                return
            if _batch_can_run_concurrently(batch, self.tool_executor):
                watermark = _last_sequence(self.runtime_session.event_log)
                await asyncio.gather(
                    *[
                        asyncio.to_thread(
                            self.tool_executor.execute,
                            call,
                            event_context=self._event_context(state),
                        )
                        for call in batch
                    ]
                )
                batch_events = _events_after(self.runtime_session.event_log, watermark)
                for event in batch_events:
                    yield event
            else:
                batch_events = []
                for call in batch:
                    watermark = _last_sequence(self.runtime_session.event_log)
                    await asyncio.to_thread(
                        self.tool_executor.execute,
                        call,
                        event_context=self._event_context(state),
                    )
                    call_events = _events_after(self.runtime_session.event_log, watermark)
                    batch_events.extend(call_events)
                    for event in call_events:
                        yield event
            for call in batch:
                result_block = _tool_result_from_event_slice(batch_events, call.id)
                _remember_tool_result_event_span(state, batch_events, call.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(role="tool_result", name=call.name, id=f"tool-result-message:{call.id}", content=[result_block])
                )
                state.tool_call_count += 1

    def _recover_or_fail_model(self, state: LoopState) -> bool:
        state.consecutive_model_failures += 1
        state.recovery_mode = True
        if state.consecutive_model_failures > self.budget.max_consecutive_model_failures:
            state.status = LoopStatus.FAILED
            state.stop_reason = "model_error"
            state.error_message = "model error budget exceeded"
            state.transition(LoopTransition.FAIL)
            return False
        state.transition(LoopTransition.CONTINUE_AFTER_RECOVERY)
        return True

    def _event_context(self, state: LoopState) -> EventContext:
        return EventContext(run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id)

    def _append_lifecycle_event(self, state: LoopState, name: str, value: dict) -> AgentEvent:
        return self.runtime_session.event_log.append(
            CustomEvent(**self._event_context(state).event_fields(), name=name, value=value)
        )


def emit_tool_result_error(
    event_log: InMemoryEventLog,
    event_context: EventContext,
    *,
    tool_call_id: str,
    tool_call_name: str,
    message: str,
    state: ToolResultState = ToolResultState.ERROR,
) -> list[AgentEvent]:
    return event_log.extend(
        [
            ToolResultStartEvent(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
            ),
            ToolResultTextDeltaEvent(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
                delta=message,
            ),
            ToolResultEndEvent(
                **event_context.event_fields(),
                tool_call_id=tool_call_id,
                state=state,
            ),
        ]
    )


def _parse_tool_call(block: ToolCallBlock) -> ToolCall:
    try:
        parsed = json.loads(block.input or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON arguments for tool {block.name}: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Tool arguments for {block.name} must be a JSON object")
    return ToolCall(id=block.id, name=block.name, arguments=parsed)


def _tool_call_blocks(message: Msg) -> list[ToolCallBlock]:
    return [block for block in message.content if isinstance(block, ToolCallBlock)]


def _tool_batches(calls: list[ToolCall], executor: ToolExecutor) -> list[list[ToolCall]]:
    batches: list[list[ToolCall]] = []
    current_readonly: list[ToolCall] = []
    for call in calls:
        if _call_can_run_concurrently(call, executor):
            current_readonly.append(call)
            continue
        if current_readonly:
            batches.append(current_readonly)
            current_readonly = []
        batches.append([call])
    if current_readonly:
        batches.append(current_readonly)
    return batches


def _batch_can_run_concurrently(calls: list[ToolCall], executor: ToolExecutor) -> bool:
    return len(calls) > 1 and all(_call_can_run_concurrently(call, executor) for call in calls)


def _call_can_run_concurrently(call: ToolCall, executor: ToolExecutor) -> bool:
    try:
        tool = executor.registry.get(call.name)
    except KeyError:
        return False
    return bool(tool.is_read_only and tool.is_concurrency_safe)


def _last_sequence(event_log: InMemoryEventLog) -> int:
    events = event_log.iter()
    return max((event.sequence or 0 for event in events), default=0)


def _events_after(event_log: InMemoryEventLog, sequence: int) -> list[AgentEvent]:
    return [event for event in event_log.iter() if (event.sequence or 0) > sequence]


def _remember_tool_result_event_span(state: LoopState, events: list[AgentEvent], tool_call_id: str) -> None:
    try:
        span = runtime_event_span_from_events(events, tool_call_id, session_id=state.session_id)
    except KeyError:
        return
    spans = state.scratchpad.setdefault("tool_result_event_spans", {})
    spans[tool_call_id] = span


def _tool_result_from_event_slice(events: list[AgentEvent], tool_call_id: str) -> ToolResultBlock:
    return completed_tool_result_from_events(events, tool_call_id)


def _accumulate_usage(state: LoopState, message: Msg) -> None:
    if message.usage is None:
        return
    state.token_usage.input_tokens += message.usage.input_tokens
    state.token_usage.output_tokens += message.usage.output_tokens
    state.token_usage.total_tokens += message.usage.total_tokens


def _final_text(messages: list[Msg]) -> str:
    for message in reversed(messages):
        if message.role != "assistant" or _tool_call_blocks(message):
            continue
        return "\n".join(block.text for block in message.content if isinstance(block, TextBlock))
    return ""


def _projection_ids(projection: dict | None) -> list[str]:
    if not projection:
        return []
    ids = projection.get("included_memory_ids")
    if isinstance(ids, list):
        return [str(item) for item in ids]
    return []


def _projection_summary(projection: dict | None) -> str:
    if not projection:
        return ""
    summary = projection.get("summary")
    return summary if isinstance(summary, str) else str(projection)
