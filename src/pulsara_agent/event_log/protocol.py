"""EventLog storage boundary."""

from __future__ import annotations

from typing import Iterable, Protocol

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.message.message import Msg


class EventLog(Protocol):
    """Append-only runtime event log contract."""

    def append(self, event: AgentEvent) -> AgentEvent: ...

    def extend(self, events: Iterable[AgentEvent]) -> list[AgentEvent]: ...

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
    ) -> list[AgentEvent]: ...

    def replay(self, reply_id: str) -> Msg: ...
