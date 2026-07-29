"""Opaque run handles and detachable observers backed by one RunOwner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pulsara_agent.ports.run_execution import (
    RunActivationOutcome,
    RunOwnerIdentity,
    RunTerminalOutcome,
)
from pulsara_agent.runtime.run_execution.owner import RunOwner, StreamObserverHandle


StopRequester = Callable[[object], Awaitable[object]]


class RegistryRunObserver:
    """A bounded observer borrow; closing it never touches the run driver."""

    def __init__(self, *, owner: RunOwner, handle: StreamObserverHandle) -> None:
        self._owner = owner
        self._handle = handle

    def __aiter__(self) -> "RegistryRunObserver":
        return self

    async def __anext__(self) -> object:
        if self._handle.state == "detached" and self._handle.queue.empty():
            raise StopAsyncIteration
        item = await self._handle.queue.get()
        if item is _OBSERVER_END:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        self._handle.detach("observer_closed")
        self._owner.observer_registry.observers.pop(self._handle.observer_id, None)


@dataclass(frozen=True, slots=True)
class RegistryRunHandle:
    """Process-local capability exposing only typed completions and observation."""

    _owner: RunOwner
    _stop_requester: StopRequester | None = None

    @property
    def identity(self) -> RunOwnerIdentity:
        return self._owner.identity

    async def wait_activation(self, activation_generation: int) -> RunActivationOutcome:
        if activation_generation < 1:
            raise ValueError("activation generation must be positive")
        existing = self._owner.activation_completion_history.get(activation_generation)
        if existing is not None:
            return existing.outcome
        active = self._owner.active_segment
        if active is None or active.segment_generation != activation_generation:
            raise KeyError(f"run has no activation generation {activation_generation}")
        result = await asyncio.shield(active.completion)
        return result.outcome

    async def wait_run_completion(self) -> RunTerminalOutcome:
        return await asyncio.shield(self._owner.run_completion)

    def subscribe(self, *, from_cursor: int | None = None) -> RegistryRunObserver:
        if from_cursor is not None and from_cursor < 0:
            raise ValueError("observer cursor must be non-negative")
        observer_id = f"run_observer:{uuid4().hex}"
        handle = StreamObserverHandle(
            observer_id=observer_id,
            queue=asyncio.Queue(maxsize=128),
            state="attached",
            detached_reason=None,
            detached=asyncio.get_running_loop().create_future(),
        )
        self._owner.observer_registry.observers[observer_id] = handle
        return RegistryRunObserver(owner=self._owner, handle=handle)

    async def request_stop(self, intent: object) -> object:
        if self._stop_requester is None:
            raise RuntimeError("run handle was not issued with stop authority")
        return await self._stop_requester(intent)


def publish_observer_event(owner: RunOwner, event: Any) -> None:
    """Best-effort bounded fan-out; a slow observer is detached, never a driver."""

    for observer_id, observer in tuple(owner.observer_registry.observers.items()):
        if observer.state == "detached":
            owner.observer_registry.observers.pop(observer_id, None)
            continue
        try:
            observer.queue.put_nowait(event)
        except asyncio.QueueFull:
            observer.state = "backpressured"
            observer.detach("observer_backpressure")
            owner.observer_registry.observers.pop(observer_id, None)


def close_observers(owner: RunOwner, *, reason: str) -> None:
    for observer in tuple(owner.observer_registry.observers.values()):
        if observer.state != "detached":
            try:
                observer.queue.put_nowait(_OBSERVER_END)
            except asyncio.QueueFull:
                pass
            observer.detach(reason)
    owner.observer_registry.observers.clear()


_OBSERVER_END = object()


__all__ = [
    "RegistryRunHandle",
    "RegistryRunObserver",
    "close_observers",
    "publish_observer_event",
]
