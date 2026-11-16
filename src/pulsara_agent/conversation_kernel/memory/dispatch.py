"""Narrow memory adapter for provider dispatch; installation owns observation state."""

from __future__ import annotations
from typing import Protocol, Callable
from .contracts import (
    MemoryObservationState,
    MemoryObservationCandidate,
    FrozenMemoryTriggerPolicy,
)

from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)


class MemoryObservationProjectionPort(Protocol):
    def classify_memory_trigger(self, text: str) -> FrozenMemoryTriggerPolicy: ...
    def bind_installed_observation_reader(
        self, reader: Callable[[], MemoryObservationState]
    ) -> None: ...
    async def prepare_memory_observation(
        self,
        query: str,
        *,
        previous_state: MemoryObservationState,
        canonical_facts=None,
        trigger_policy=None,
    ) -> MemoryObservationCandidate: ...


class MemoryDispatchSupport:
    def __init__(
        self, *, compiler, io_owner, memory_projection, input_reader, deadline_factory
    ):
        self._projection = memory_projection

    @property
    def available(self):
        return self._projection is not None

    def classify_trigger(self, text):
        if self._projection is None:
            raise RuntimeError("memory projection is not installed")
        return self._projection.classify_memory_trigger(text)

    def freeze_call_context(self, *, memory_use_policy):
        return FrozenModelCallMemoryContext(memory_use_policy)

    def bind_installed_observation_reader(self, reader):
        if self._projection is not None:
            self._projection.bind_installed_observation_reader(reader)

    async def prepare_observation(
        self, query, *, previous_state, canonical_facts=None, trigger_policy=None
    ):
        if self._projection is None:
            raise RuntimeError("memory projection is not installed")
        return await self._projection.prepare_memory_observation(
            query,
            previous_state=previous_state,
            canonical_facts=canonical_facts,
            trigger_policy=trigger_policy,
        )
