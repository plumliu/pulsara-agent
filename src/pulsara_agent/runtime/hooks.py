"""Extension hooks for runtime memory integration."""

from __future__ import annotations

from typing import Any, Protocol

from pulsara_agent.message import Msg, ToolResultBlock
from pulsara_agent.runtime.state import LoopState


class MemoryHooks(Protocol):
    async def on_session_start(self, state: LoopState, user_input: str) -> None: ...

    async def project(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None: ...

    async def after_model_reply(self, state: LoopState, assistant: Msg) -> None: ...

    async def after_tool_results(self, state: LoopState, results: list[ToolResultBlock]) -> None: ...

    async def should_compact(self, state: LoopState) -> bool: ...

    async def on_session_end(self, state: LoopState) -> None: ...


class ToolResultPersistenceHook(Protocol):
    async def after_tool_results(self, state: LoopState, results: list[ToolResultBlock]) -> None: ...


class NoopMemoryHooks:
    async def on_session_start(self, state: LoopState, user_input: str) -> None:
        return None

    async def project(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None:
        return None

    async def after_model_reply(self, state: LoopState, assistant: Msg) -> None:
        return None

    async def after_tool_results(self, state: LoopState, results: list[ToolResultBlock]) -> None:
        return None

    async def should_compact(self, state: LoopState) -> bool:
        return False

    async def on_session_end(self, state: LoopState) -> None:
        return None
