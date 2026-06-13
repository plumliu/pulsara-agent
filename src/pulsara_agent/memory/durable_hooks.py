"""Durable-memory producer hook.

Bridges the agent loop to the durable-memory write path. Memory candidates are
deposited into a :class:`MemoryProposalSink` from tool-execution threads (by the
``propose_memory`` tool); this hook drains them at agent-loop-safe points and
routes each through :meth:`MemoryWriteService.submit`. The hook never emits --
it *returns* the events ``submit`` produced and lets ``AgentRuntime`` emit them
with a runtime-managed sequence, keeping publish off the tool thread.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.memory.write_service import MemoryWriteService
from pulsara_agent.message import Msg, ToolResultBlock
from pulsara_agent.runtime.hooks import NoopMemoryHooks
from pulsara_agent.runtime.proposal_sink import MemoryProposalSink
from pulsara_agent.runtime.state import LoopState


@dataclass(slots=True)
class DurableMemoryHooks(NoopMemoryHooks):
    service: MemoryWriteService
    sink: MemoryProposalSink

    @property
    def memory_proposal_sink(self) -> MemoryProposalSink | None:
        return self.sink

    async def after_model_reply(self, state: LoopState, assistant: Msg) -> list[AgentEvent]:
        return self._drain(state)

    async def after_tool_results(
        self, state: LoopState, results: list[ToolResultBlock]
    ) -> list[AgentEvent]:
        return self._drain(state)

    async def on_session_end(self, state: LoopState) -> list[AgentEvent]:
        return self._drain(state)

    def _drain(self, state: LoopState) -> list[AgentEvent]:
        candidates = self.sink.drain()
        if not candidates:
            return []
        event_context = EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
        events: list[AgentEvent] = []
        for candidate in candidates:
            outcome = self.service.submit(candidate, event_context=event_context)
            events.extend(outcome.events)
        return events
