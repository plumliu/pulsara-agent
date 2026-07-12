"""EventLog storage boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from pulsara_agent.event.events import AgentEvent
from pulsara_agent.message.message import Msg


class EventIdConflict(RuntimeError):
    """An event id already names a different immutable event payload."""

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"Event id already belongs to a different event: {event_id}")


class EventLogWriteConflict(RuntimeError):
    """Conditional append observed a different session high-water mark."""

    def __init__(self, *, expected_last_sequence: int, actual_last_sequence: int) -> None:
        self.expected_last_sequence = expected_last_sequence
        self.actual_last_sequence = actual_last_sequence
        super().__init__(
            "EventLog conditional write conflict: "
            f"expected last sequence {expected_last_sequence}, actual {actual_last_sequence}"
        )


@dataclass(frozen=True, slots=True)
class EventBatchConfirmation:
    committed_events: tuple[AgentEvent, ...]
    missing_event_ids: tuple[str, ...]
    actual_last_sequence: int


class EventLog(Protocol):
    """Append-only runtime event log contract."""

    def ensure_runtime_session_owner(self) -> None:
        """Ensure the durable session owner exists before pre-event artifacts."""
        ...

    def append(
        self,
        event: AgentEvent,
        *,
        expected_last_sequence: int | None = None,
    ) -> AgentEvent: ...

    def extend(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
    ) -> list[AgentEvent]: ...

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]: ...

    def get_by_id(self, event_id: str) -> AgentEvent | None: ...

    def confirm_batch(self, candidates: Sequence[AgentEvent]) -> EventBatchConfirmation: ...

    def replay(self, reply_id: str) -> Msg: ...

    def next_sequence(self) -> int: ...


def same_event_payload(candidate: AgentEvent, stored: AgentEvent) -> bool:
    """Compare one immutable event fact while ignoring its assigned sequence."""

    if candidate.id != stored.id:
        return False
    return candidate.model_dump(mode="json", exclude={"sequence"}) == stored.model_dump(
        mode="json",
        exclude={"sequence"},
    )
