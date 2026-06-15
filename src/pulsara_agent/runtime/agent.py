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
    ProjectionFailedEvent,
    ProjectionReadyEvent,
    ProjectionRequestedEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory.provenance import runtime_event_span_from_events
from pulsara_agent.message import (
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from pulsara_agent.message.assembler import completed_tool_result_from_events
from pulsara_agent.runtime.context import build_llm_context
from pulsara_agent.runtime.hooks import MemoryHooks, NoopMemoryHooks, ToolResultPersistenceHook
from pulsara_agent.runtime.publisher import RuntimeEventSubscriber, RuntimePublishedEvent
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


class _ToolBatchTap(RuntimeEventSubscriber):
    def __init__(self, tool_call_ids: set[str]) -> None:
        self._tool_call_ids = tool_call_ids
        self.queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        event = published.event
        tool_call_id = getattr(event, "tool_call_id", None)
        if tool_call_id not in self._tool_call_ids:
            return
        if isinstance(
            event,
            (
                ToolResultStartEvent,
                ToolResultTextDeltaEvent,
                ToolResultDataDeltaEvent,
                ToolResultEndEvent,
            ),
        ):
            await self.queue.put(event)


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
        self.tool_executor = runtime_session.create_tool_executor(
            memory_proposal_sink=getattr(self.memory_hooks, "memory_proposal_sink", None),
            memory_recall_service=getattr(self.memory_hooks, "recall", None),
            memory_query=getattr(self.memory_hooks, "memory_query", None),
            graph_id=getattr(self.memory_hooks, "graph_id", None),
        )
        self._last_result: AgentRunResult | None = None

    async def run_task(self, user_input: str) -> AgentRunResult:
        async for _event in self.stream_task(user_input):
            pass
        assert self._last_result is not None
        return self._last_result

    async def stream_task(self, user_input: str) -> AsyncIterator[AgentEvent]:
        state = LoopState(session_id=self.runtime_session.runtime_session_id, budget=self.budget)
        state.messages.append(UserMsg(name="user", content=user_input))
        yield await self.runtime_session.emit(
            RunStartEvent(
                **self._event_context(state).event_fields(),
                user_input_chars=len(user_input),
            ),
            state=state,
        )
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
                state.status = LoopStatus.FAILED
                state.stop_reason = "max_turns"
                state.transition(LoopTransition.EXCEED_MAX_ITERS)
                event = await self.runtime_session.emit(
                    ExceedMaxItersEvent(
                        **self._event_context(state).event_fields(),
                        name="agent_runtime",
                        max_iters=self.budget.max_turns,
                    ),
                    state=state,
                )
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
                    stored = await self.runtime_session.emit(event, state=state)
                    if isinstance(stored, RunErrorEvent):
                        reply_had_run_error = True
                    yield stored
            except Exception as exc:
                event = await self.runtime_session.emit(
                    RunErrorEvent(
                        **self._event_context(state).event_fields(),
                        message=f"{type(exc).__name__}: {exc}",
                        code="model_stream_error",
                    ),
                    state=state,
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
            ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "after_model_reply",
                lambda: self.memory_hooks.after_model_reply(state, assistant),
            )
            for event in hook_events:
                yield event
            if not ok:
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
            ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "after_tool_results",
                lambda: self.memory_hooks.after_tool_results(state, state.tool_results),
            )
            for event in hook_events:
                yield event
            if not ok:
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
                yield await self.runtime_session.emit(
                    CustomEvent(
                        **self._event_context(state).event_fields(),
                        name="compaction_requested",
                        value={},
                    ),
                    state=state,
                )
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
            _ok, hook_events = await self._run_memory_hook_and_emit_events(
                state,
                "on_session_end",
                lambda: self.memory_hooks.on_session_end(state),
            )
            for event in hook_events:
                yield event
        yield await self.runtime_session.emit(
            RunEndEvent(
                **self._event_context(state).event_fields(),
                status=state.status.value,
                stop_reason=state.stop_reason,
                error_message=state.error_message,
            ),
            state=state,
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
            event = await self._mark_memory_hook_failed(state, hook_name, exc)
            return False, None, event

    async def _run_memory_hook_and_emit_events(
        self,
        state: LoopState,
        hook_name: str,
        call,
    ) -> tuple[bool, list[AgentEvent]]:
        ok, produced_events, error_event = await self._run_memory_hook(state, hook_name, call)
        if not ok:
            assert error_event is not None
            return False, [error_event]
        emitted_events: list[AgentEvent] = []
        try:
            for event in produced_events or ():
                emitted_events.append(await self.runtime_session.emit(event, state=state))
        except Exception as exc:
            emitted_events.append(await self._mark_memory_hook_failed(state, hook_name, exc))
            return False, emitted_events
        return True, emitted_events

    async def _run_tool_result_persistence_hook(self, state: LoopState) -> AgentEvent | None:
        assert self.tool_result_persistence_hook is not None
        try:
            await self.tool_result_persistence_hook.after_tool_results(state, state.tool_results)
            return None
        except Exception as exc:
            return await self.runtime_session.emit(
                CustomEvent(
                    **self._event_context(state).event_fields(),
                    name="tool_result_persistence_failed",
                    value={
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
                state=state,
            )

    async def _mark_memory_hook_failed(self, state: LoopState, hook_name: str, exc: Exception) -> AgentEvent:
        message = f"memory hook {hook_name} failed: {type(exc).__name__}: {exc}"
        state.status = LoopStatus.FAILED
        state.stop_reason = "memory_hook_error"
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="memory_hook_error",
                metadata={"hook": hook_name},
            ),
            state=state,
        )

    async def _mark_tool_budget_exceeded(self, state: LoopState, *, attempted_count: int) -> AgentEvent:
        message = (
            "tool call budget exceeded before execution: "
            f"current={state.tool_call_count}, attempted={attempted_count}, max={self.budget.max_tool_calls}"
        )
        state.status = LoopStatus.FAILED
        state.stop_reason = "tool_error_budget"
        state.error_message = message
        state.transition(LoopTransition.FAIL)
        return await self.runtime_session.emit(
            RunErrorEvent(
                **self._event_context(state).event_fields(),
                message=message,
                code="tool_budget_exceeded",
                metadata={
                    "current_tool_call_count": state.tool_call_count,
                    "attempted_tool_call_count": attempted_count,
                    "max_tool_calls": self.budget.max_tool_calls,
                },
            ),
            state=state,
        )

    async def _project_memory(self, state: LoopState) -> AsyncIterator[AgentEvent]:
        projection_id = f"projection:{state.turn_id}"
        context = self._event_context(state)
        yield await self.runtime_session.emit(
            ProjectionRequestedEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
            ),
            state=state,
        )
        try:
            projection = await asyncio.wait_for(
                self.memory_hooks.project(
                    state,
                    token_budget=self.budget.projection_token_budget,
                ),
                timeout=self.budget.recall_hard_timeout_ms / 1000,
            )
        except TimeoutError:
            state.memory_projection = None
            yield await self.runtime_session.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error="recall_timeout",
                ),
                state=state,
            )
            return
        except Exception as exc:
            state.memory_projection = None
            yield await self.runtime_session.emit(
                ProjectionFailedEvent(
                    **context.event_fields(),
                    projection_id=projection_id,
                    role=self.model_role.value,
                    scope=state.current_scope or "session",
                    token_budget=self.budget.projection_token_budget,
                    error=f"{type(exc).__name__}: {exc}",
                ),
                state=state,
            )
            return
        state.memory_projection = projection
        yield await self.runtime_session.emit(
            ProjectionReadyEvent(
                **context.event_fields(),
                projection_id=projection_id,
                role=self.model_role.value,
                scope=state.current_scope or "session",
                token_budget=self.budget.projection_token_budget,
                included_memory_ids=_projection_ids(projection),
                summary=_projection_summary(projection),
            ),
            state=state,
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
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=block.id,
                        tool_call_name=block.name,
                        message=str(exc),
                    ),
                    state=state,
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

        duplicate_ids = _duplicate_tool_call_ids(parsed_calls)
        if duplicate_ids:
            unique_calls: list[ToolCall] = []
            for call in parsed_calls:
                if call.id not in duplicate_ids:
                    unique_calls.append(call)
                    continue
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=f"Duplicate tool_call_id in assistant reply: {call.id}",
                    ),
                    state=state,
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
            parsed_calls = unique_calls
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
            state.status = LoopStatus.WAITING_USER
            state.stop_reason = "waiting_user"
            state.transition(LoopTransition.WAIT_FOR_USER)
            event = await self.runtime_session.emit(
                RequireUserConfirmEvent(**self._event_context(state).event_fields(), tool_calls=blocks),
                state=state,
            )
            yield event
            return
        if decision.kind is PermissionDecisionKind.DENY:
            for call in parsed_calls:
                stored_events = await self.runtime_session.emit_many(
                    build_tool_result_error_events(
                        self._event_context(state),
                        tool_call_id=call.id,
                        tool_call_name=call.name,
                        message=decision.reason or "tool call denied by permission gate",
                        state=ToolResultState.DENIED,
                    ),
                    state=state,
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
                yield await self._mark_tool_budget_exceeded(state, attempted_count=len(batch))
                return
            batch_events: list[AgentEvent] = []
            async for event in self._stream_tool_batch_events(state, batch, batch_events):
                yield event
            for call in batch:
                result_block = _tool_result_from_event_slice(batch_events, call.id)
                _remember_tool_result_event_span(state, batch_events, call.id)
                state.tool_results.append(result_block)
                state.messages.append(
                    Msg(role="tool_result", name=call.name, id=f"tool-result-message:{call.id}", content=[result_block])
                )
                state.tool_call_count += 1

    async def _stream_tool_batch_events(
        self,
        state: LoopState,
        batch: list[ToolCall],
        batch_events: list[AgentEvent],
    ) -> AsyncIterator[AgentEvent]:
        tap = _ToolBatchTap({call.id for call in batch})
        self.runtime_session.publisher.subscribe(tap)
        executor = ToolExecutor(
            registry=self.tool_executor.registry,
            record_event=self.runtime_session.make_thread_recorder(state=state),
        )
        tasks = [
            asyncio.create_task(
                asyncio.to_thread(
                    executor.execute,
                    call,
                    event_context=self._event_context(state),
                )
            )
            for call in batch
        ]
        pending = set(tasks)
        completed_tool_calls: set[str] = set()

        try:
            while pending or len(completed_tool_calls) < len(batch) or not tap.queue.empty():
                while not tap.queue.empty():
                    event = tap.queue.get_nowait()
                    batch_events.append(event)
                    if isinstance(event, ToolResultEndEvent):
                        completed_tool_calls.add(event.tool_call_id)
                    yield event
                if pending:
                    done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    continue
                if len(completed_tool_calls) < len(batch):
                    event = await tap.queue.get()
                    batch_events.append(event)
                    if isinstance(event, ToolResultEndEvent):
                        completed_tool_calls.add(event.tool_call_id)
                    yield event
        finally:
            self.runtime_session.publisher.unsubscribe(tap)
            for task in pending:
                if not task.done():
                    task.cancel()

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

def build_tool_result_error_events(
    event_context: EventContext,
    *,
    tool_call_id: str,
    tool_call_name: str,
    message: str,
    state: ToolResultState = ToolResultState.ERROR,
) -> list[AgentEvent]:
    return [
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


def _duplicate_tool_call_ids(calls: list[ToolCall]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for call in calls:
        if call.id in seen:
            duplicates.add(call.id)
            continue
        seen.add(call.id)
    return duplicates


def _call_can_run_concurrently(call: ToolCall, executor: ToolExecutor) -> bool:
    try:
        tool = executor.registry.get(call.name)
    except KeyError:
        return False
    return bool(tool.is_read_only and tool.is_concurrency_safe)


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
