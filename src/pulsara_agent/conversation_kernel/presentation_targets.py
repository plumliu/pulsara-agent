"""Shared, read-only object location for Desktop navigation and presentation tools."""
from dataclasses import dataclass
from typing import Literal

from psycopg import InterfaceError, OperationalError

from pulsara_agent.identity import require_id
from pulsara_agent.storage.migrations.errors import PostgresSchemaError, PostgresSchemaFailureCode


class PresentationReadUnavailable(RuntimeError):
    """A known read-owner outage, distinct from retirement or a programming error."""


@dataclass(frozen=True, slots=True)
class ResolvedPresentationObject:
    kind: Literal["AGENT", "CELL"]
    target_id: str
    root_session_id: str
    host_session_id: str | None = None


def resolve_visible_agent(repository, *, root_session_id: str, agent_session_id: str,
                          deadline_monotonic: float):
    """Read current membership without acquiring a writer or refreshing idle time."""
    require_id(agent_session_id, "session")
    if agent_session_id == root_session_id:
        raise LookupError("ROOT cannot be opened as a Subagent")
    try:
        state = repository.read_agent_runtime_state(
            session_id=agent_session_id, deadline_monotonic=deadline_monotonic)
    except (TimeoutError, ConnectionError, InterfaceError, OperationalError) as error:
        raise PresentationReadUnavailable("Agent reader is temporarily unavailable") from error
    except PostgresSchemaError as error:
        if error.code not in {
            PostgresSchemaFailureCode.CONNECTION_FAILED,
            PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            PostgresSchemaFailureCode.ACCESS_LEASE_RELEASED,
        }:
            raise
        raise PresentationReadUnavailable("Agent reader is temporarily unavailable") from error
    if state is None or state["root_session_id"] != root_session_id:
        raise LookupError("Agent is not available in this ROOT")
    return state


def resolve_visible_cell(owner, *, cell_id: str):
    """Capture only a current Host's Cell; never observe or consume its output."""
    require_id(cell_id, "cell")
    return owner.capture_code_cell(cell_id)
