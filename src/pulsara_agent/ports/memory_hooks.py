"""Capability-scoped inputs for memory hooks.

Memory integrations receive an immutable projection of one run activation.
They never receive the mutable runtime working state or its execution owners.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, Sequence, TypeAlias

from pydantic import Field

from pulsara_agent.event import AgentEvent
from pulsara_agent.message import Msg, ToolResultBlock, Usage
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase


MemoryHookRunStatus: TypeAlias = Literal[
    "running",
    "waiting_user",
    "finished",
    "failed",
    "aborted",
]


class MemoryHookUsageView(FrozenRuntimeStateBase):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class MemoryHookRunView(FrozenRuntimeStateBase):
    """Bounded, recursively immutable input for one memory hook call."""

    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    reply_id: str = Field(min_length=1)
    status: MemoryHookRunStatus
    frozen_messages: tuple[FrozenJsonObjectFact, ...]
    usage: MemoryHookUsageView
    current_projection: FrozenJsonObjectFact | None = None
    model_step_key: str = Field(min_length=1)

    @property
    def session_id(self) -> str:
        return self.runtime_session_id

    @property
    def messages(self) -> tuple[Msg, ...]:
        return tuple(
            Msg.model_validate(thaw_json(message)) for message in self.frozen_messages
        )

    @property
    def token_usage(self) -> Usage:
        return Usage(
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
            total_tokens=self.usage.total_tokens,
        )

    @property
    def memory_projection(self) -> dict[str, Any] | None:
        if self.current_projection is None:
            return None
        value = thaw_json(self.current_projection)
        if not isinstance(value, dict):
            raise TypeError("memory projection must decode to an object")
        return value


def build_memory_hook_run_view(
    *,
    runtime_session_id: str,
    run_id: str,
    turn_id: str,
    reply_id: str,
    status: MemoryHookRunStatus,
    messages: Sequence[Msg],
    usage: Usage,
    current_projection: dict[str, Any] | None,
    model_step_key: str,
) -> MemoryHookRunView:
    frozen_messages: list[FrozenJsonObjectFact] = []
    for message in messages:
        frozen = freeze_json(message.model_dump(mode="json"))
        if not isinstance(frozen, FrozenJsonObjectFact):
            raise TypeError("message projection must be an object")
        frozen_messages.append(frozen)
    frozen_projection = (
        freeze_json(current_projection) if current_projection is not None else None
    )
    if frozen_projection is not None and not isinstance(
        frozen_projection, FrozenJsonObjectFact
    ):
        raise TypeError("memory projection must be an object")
    return MemoryHookRunView(
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        turn_id=turn_id,
        reply_id=reply_id,
        status=status,
        frozen_messages=tuple(frozen_messages),
        usage=MemoryHookUsageView(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        ),
        current_projection=frozen_projection,
        model_step_key=model_step_key,
    )


class MemoryHooks(Protocol):
    @property
    def memory_proposal_sink(self) -> object | None: ...

    async def on_turn_start(self, view: MemoryHookRunView, user_input: str) -> None: ...

    async def on_session_start(
        self, view: MemoryHookRunView, user_input: str
    ) -> None: ...

    def baseline_projection(
        self, view: MemoryHookRunView, *, token_budget: int
    ) -> dict[str, Any] | None: ...

    async def project(
        self, view: MemoryHookRunView, *, token_budget: int
    ) -> dict[str, Any] | None: ...

    async def after_model_reply(
        self, view: MemoryHookRunView, assistant: Msg
    ) -> list[AgentEvent]: ...

    async def after_tool_results(
        self, view: MemoryHookRunView, results: list[ToolResultBlock]
    ) -> list[AgentEvent]: ...

    async def should_compact(self, view: MemoryHookRunView) -> bool: ...

    async def on_turn_end(self, view: MemoryHookRunView) -> list[AgentEvent]: ...

    async def on_session_end(self, view: MemoryHookRunView) -> list[AgentEvent]: ...


class ToolResultPersistenceHook(Protocol):
    async def after_tool_results(
        self, view: MemoryHookRunView, results: list[ToolResultBlock]
    ) -> None: ...


class NoopMemoryHooks:
    @property
    def memory_proposal_sink(self) -> object | None:
        return None

    async def on_session_start(self, view: MemoryHookRunView, user_input: str) -> None:
        return None

    async def on_turn_start(self, view: MemoryHookRunView, user_input: str) -> None:
        return await self.on_session_start(view, user_input)

    def baseline_projection(
        self, view: MemoryHookRunView, *, token_budget: int
    ) -> dict[str, Any] | None:
        return None

    async def project(
        self, view: MemoryHookRunView, *, token_budget: int
    ) -> dict[str, Any] | None:
        return None

    async def after_model_reply(
        self, view: MemoryHookRunView, assistant: Msg
    ) -> list[AgentEvent]:
        return []

    async def after_tool_results(
        self, view: MemoryHookRunView, results: list[ToolResultBlock]
    ) -> list[AgentEvent]:
        return []

    async def should_compact(self, view: MemoryHookRunView) -> bool:
        return False

    async def on_session_end(self, view: MemoryHookRunView) -> list[AgentEvent]:
        return []

    async def on_turn_end(self, view: MemoryHookRunView) -> list[AgentEvent]:
        return await self.on_session_end(view)


__all__ = [
    "MemoryHookRunStatus",
    "MemoryHookRunView",
    "MemoryHookUsageView",
    "MemoryHooks",
    "NoopMemoryHooks",
    "ToolResultPersistenceHook",
    "build_memory_hook_run_view",
]
