"""Shared SQL facts for a request's execution in an inspector snapshot."""

from __future__ import annotations

from typing import Mapping

from pulsara_agent.conversation_kernel.contracts import TurnStatus
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.conversation_kernel.request_execution import (
    NewTurnExecutionFacts,
    RequestExecution,
    project_request_execution,
)


# Queries using these columns join q -> qe -> qt by session and exact entry ID.
REQUEST_EXECUTION_COLUMNS = """
    q.id AS execution_queue_id,
    q.delivery_mode AS execution_delivery_mode,
    q.status AS execution_queue_status,
    q.terminal_reason AS execution_queue_reason,
    q.consumed_entry_id AS execution_consumed_entry_id,
    qe.id AS execution_entry_id,
    qe.entry_kind AS execution_entry_kind,
    qe.turn_id AS execution_entry_turn_id,
    qt.id AS execution_turn_id,
    qt.initial_entry_id AS execution_initial_entry_id,
    qt.status AS execution_turn_status,
    qt.terminal_reason AS execution_turn_reason
"""


def request_execution_from_row(row: Mapping[str, object]) -> RequestExecution | None:
    """Read only the shared projection columns, not latest-turn/command state."""
    mode = row["execution_delivery_mode"]
    if mode == "STEER_ACTIVE_TURN":
        return None
    if mode != "NEW_TURN" or row["execution_queue_id"] is None:
        raise ConversationKernelConflict("request execution query did not resolve a NEW_TURN queue")
    consumed = row["execution_consumed_entry_id"]
    if row["execution_queue_status"] == "CONSUMED":
        if (
            consumed is None
            or row["execution_entry_id"] != consumed
            or row["execution_entry_kind"] != "USER_MESSAGE"
            or row["execution_turn_id"] is None
            or row["execution_entry_turn_id"] != row["execution_turn_id"]
            or row["execution_initial_entry_id"] != consumed
        ):
            raise ConversationKernelConflict("consumed request does not identify its exact initial turn entry")
    try:
        return project_request_execution(NewTurnExecutionFacts(
            queue_status=str(row["execution_queue_status"]),
            queue_terminal_reason=row["execution_queue_reason"],
            consumed_entry_id=consumed,
            turn_status=(
                None if row["execution_turn_status"] is None
                else TurnStatus(row["execution_turn_status"])
            ),
            turn_terminal_reason=row["execution_turn_reason"],
        ))
    except ValueError as exc:
        raise ConversationKernelConflict("request execution facts are inconsistent") from exc
