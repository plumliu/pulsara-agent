"""Extension hooks for Pulsara runtime integration."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol, TypeAlias

from pulsara_agent.event import AgentEvent, EventType
from pulsara_agent.message import Msg, ToolResultBlock
from pulsara_agent.message.assembler import BlockAssembler, BlockCompletion
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.runtime.state import LoopState


@dataclass(slots=True)
class HookContext:
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    state: LoopState | None = None


@dataclass(slots=True)
class ObserverHookResult:
    metadata: dict[str, Any] = field(default_factory=dict)


class HookDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    MODIFY = "modify"
    REQUEST_USER = "request_user"
    APPEND_CONTEXT = "append_context"


@dataclass(slots=True)
class ControlHookResult:
    decision: HookDecision
    value: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass(slots=True)
class HookDispatchError:
    hook_kind: str
    selector: str | None
    handler_name: str
    error_type: str
    message: str
    run_id: str
    turn_id: str
    reply_id: str
    event_id: str | None = None
    block_id: str | None = None


MaybeAwaitable: TypeAlias = Any
EventObserverHook: TypeAlias = Callable[[HookContext, AgentEvent], MaybeAwaitable]
BlockObserverHook: TypeAlias = Callable[[HookContext, BlockCompletion], MaybeAwaitable]


@dataclass(slots=True)
class _EventHookRegistration:
    selector: EventType | None
    handler: EventObserverHook


@dataclass(slots=True)
class _BlockHookRegistration:
    selector: str | None
    handler: BlockObserverHook


class RuntimeHookManager:
    """Internal observer hook bus for runtime events and completed blocks."""

    def __init__(self) -> None:
        self._event_hooks: list[_EventHookRegistration] = []
        self._block_hooks: list[_BlockHookRegistration] = []
        self._assembler = BlockAssembler()
        self.errors: list[HookDispatchError] = []

    def register_event(self, event_type: EventType | None, handler: EventObserverHook) -> None:
        self._event_hooks.append(_EventHookRegistration(selector=event_type, handler=handler))

    def register_block(self, block_type: str | None, handler: BlockObserverHook) -> None:
        self._block_hooks.append(_BlockHookRegistration(selector=block_type, handler=handler))

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        context = HookContext(
            runtime_session_id=published.runtime_session_id,
            run_id=published.event.run_id,
            turn_id=published.event.turn_id,
            reply_id=published.event.reply_id,
            state=published.state,
        )
        await self.dispatch_observer_event(context, published.event)

    async def dispatch_observer_event(self, context: HookContext, event: AgentEvent) -> None:
        for registration in self._event_hooks:
            if registration.selector is not None and registration.selector != event.type:
                continue
            try:
                await _maybe_await(registration.handler(context, event.model_copy(deep=True)))
            except Exception as exc:
                self.errors.append(
                    _dispatch_error(
                        context,
                        hook_kind="event",
                        selector=registration.selector.value if registration.selector is not None else None,
                        handler=registration.handler,
                        exc=exc,
                        event_id=event.id,
                    )
                )

        update = self._assembler.append(event)
        for completion in update.completed:
            await self.dispatch_observer_block(context, completion)
        if event.type in _REPLY_CLEANUP_EVENT_TYPES:
            self._assembler.discard_reply(event.reply_id)

    async def dispatch_observer_block(self, context: HookContext, completion: BlockCompletion) -> None:
        for registration in self._block_hooks:
            if registration.selector is not None and registration.selector != completion.block_type:
                continue
            try:
                await _maybe_await(registration.handler(context, completion))
            except Exception as exc:
                self.errors.append(
                    _dispatch_error(
                        context,
                        hook_kind="block",
                        selector=registration.selector,
                        handler=registration.handler,
                        exc=exc,
                        block_id=completion.block_id,
                    )
                )


async def _maybe_await(value: MaybeAwaitable) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


_REPLY_CLEANUP_EVENT_TYPES = {
    EventType.REPLY_END,
    EventType.RUN_ERROR,
    EventType.EXCEED_MAX_ITERS,
}


def _handler_name(handler: Callable[..., Any]) -> str:
    return getattr(handler, "__qualname__", getattr(handler, "__name__", repr(handler)))


def _dispatch_error(
    context: HookContext,
    *,
    hook_kind: str,
    selector: str | None,
    handler: Callable[..., Any],
    exc: Exception,
    event_id: str | None = None,
    block_id: str | None = None,
) -> HookDispatchError:
    return HookDispatchError(
        hook_kind=hook_kind,
        selector=selector,
        handler_name=_handler_name(handler),
        error_type=type(exc).__name__,
        message=str(exc),
        run_id=context.run_id,
        turn_id=context.turn_id,
        reply_id=context.reply_id,
        event_id=event_id,
        block_id=block_id,
    )


class MemoryHooks(Protocol):
    @property
    def memory_proposal_sink(self) -> MemoryProposalSink | None: ...

    async def on_turn_start(self, state: LoopState, user_input: str) -> None: ...

    async def on_session_start(self, state: LoopState, user_input: str) -> None: ...

    def baseline_projection(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None: ...

    async def project(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None: ...

    async def after_model_reply(self, state: LoopState, assistant: Msg) -> list[AgentEvent]: ...

    async def after_tool_results(
        self, state: LoopState, results: list[ToolResultBlock]
    ) -> list[AgentEvent]: ...

    async def should_compact(self, state: LoopState) -> bool: ...

    async def on_turn_end(self, state: LoopState) -> list[AgentEvent]: ...

    async def on_session_end(self, state: LoopState) -> list[AgentEvent]: ...


class ToolResultPersistenceHook(Protocol):
    async def after_tool_results(self, state: LoopState, results: list[ToolResultBlock]) -> None: ...


class NoopMemoryHooks:
    @property
    def memory_proposal_sink(self) -> MemoryProposalSink | None:
        return None

    async def on_session_start(self, state: LoopState, user_input: str) -> None:
        return None

    async def on_turn_start(self, state: LoopState, user_input: str) -> None:
        return await self.on_session_start(state, user_input)

    def baseline_projection(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None:
        return None

    async def project(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None:
        return None

    async def after_model_reply(self, state: LoopState, assistant: Msg) -> list[AgentEvent]:
        return []

    async def after_tool_results(
        self, state: LoopState, results: list[ToolResultBlock]
    ) -> list[AgentEvent]:
        return []

    async def should_compact(self, state: LoopState) -> bool:
        return False

    async def on_session_end(self, state: LoopState) -> list[AgentEvent]:
        return []

    async def on_turn_end(self, state: LoopState) -> list[AgentEvent]:
        return await self.on_session_end(state)
