"""Pure diagnostic rules for Pulsara Inspector."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from pulsara_agent.event import (
    AgentEvent,
    RunEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from pulsara_agent.runtime.tool_taxonomy import PLAN_WORKFLOW_TOOL_NAMES


def sequence_gap_diagnostics(events: Iterable[AgentEvent]) -> list[dict[str, Any]]:
    ordered = sorted(
        [event for event in events if event.sequence is not None],
        key=lambda event: event.sequence or 0,
    )
    diagnostics: list[dict[str, Any]] = []
    previous: int | None = None
    for event in ordered:
        assert event.sequence is not None
        if previous is not None and event.sequence != previous + 1:
            diagnostics.append(
                {
                    "code": "sequence_gap",
                    "severity": "error",
                    "message": "Session event sequence is not contiguous.",
                    "details": {
                        "after_sequence": previous,
                        "before_sequence": event.sequence,
                        "missing_count": event.sequence - previous - 1,
                    },
                }
            )
        previous = event.sequence
    return diagnostics


def run_projection_diagnostics(run_row: dict[str, Any] | None, events: Iterable[AgentEvent]) -> list[dict[str, Any]]:
    if run_row is None:
        return [
            {
                "code": "missing_run_row",
                "severity": "error",
                "message": "Run exists in events but has no runs parent row.",
                "details": {},
            }
        ]
    latest_end = _latest_run_end(events)
    if latest_end is None:
        return []
    stale_fields: dict[str, Any] = {}
    if run_row.get("status") != latest_end.status:
        stale_fields["status"] = {"run_row": run_row.get("status"), "event": latest_end.status}
    if run_row.get("stop_reason") != latest_end.stop_reason:
        stale_fields["stop_reason"] = {"run_row": run_row.get("stop_reason"), "event": latest_end.stop_reason}
    completed_at = run_row.get("completed_at")
    if completed_at is None:
        stale_fields["completed_at"] = {"run_row": None, "event": latest_end.created_at}
    elif _same_instant(completed_at, latest_end.created_at) is False:
        stale_fields["completed_at"] = {"run_row": completed_at, "event": latest_end.created_at}
    if not stale_fields:
        return []
    return [
        {
            "code": "run_projection_stale",
            "severity": "warning",
            "message": "runs summary row does not match canonical RUN_END event.",
            "details": {"run_id": latest_end.run_id, "fields": stale_fields},
        }
    ]


def tool_flow_diagnostics(
    events: Iterable[AgentEvent],
    *,
    known_artifact_ids: set[str],
) -> list[dict[str, Any]]:
    proposed: dict[str, ToolCallStartEvent] = {}
    completed: set[str] = set()
    run_end_sequence: int | None = None
    diagnostics: list[dict[str, Any]] = []

    for event in events:
        if isinstance(event, ToolCallStartEvent):
            proposed[event.tool_call_id] = event
        elif isinstance(event, RunEndEvent):
            if event.sequence is not None:
                run_end_sequence = event.sequence
        elif isinstance(event, ToolResultEndEvent):
            completed.add(event.tool_call_id)
            if run_end_sequence is not None and event.sequence is not None and event.sequence > run_end_sequence:
                diagnostics.append(
                    {
                        "code": "late_tool_result",
                        "severity": "warning",
                        "message": "Tool result ended after the run terminal event.",
                        "details": {
                            "tool_call_id": event.tool_call_id,
                            "run_end_sequence": run_end_sequence,
                            "tool_result_sequence": event.sequence,
                        },
                    }
                )
            for ref in event.artifacts:
                if ref.artifact_id not in known_artifact_ids:
                    diagnostics.append(
                        {
                            "code": "missing_artifact",
                            "severity": "error",
                            "message": "Tool result references an artifact that is not in the artifact store/index.",
                            "details": {
                                "tool_call_id": event.tool_call_id,
                                "artifact_id": ref.artifact_id,
                            },
                        }
                    )

    for tool_call_id, event in proposed.items():
        if tool_call_id in completed:
            continue
        if event.tool_call_name in PLAN_WORKFLOW_TOOL_NAMES:
            continue
        diagnostics.append(
            {
                "code": "orphan_tool_call",
                "severity": "warning",
                "message": "Tool call has no completed tool result in this run.",
                "details": {
                    "tool_call_id": tool_call_id,
                    "tool_name": event.tool_call_name,
                    "sequence": event.sequence,
                },
            }
        )
    return diagnostics


def outbox_diagnostics(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        status = row.get("status")
        if status not in {"failed", "pending"}:
            continue
        diagnostics.append(
            {
                "code": "outbox_failed" if status == "failed" else "outbox_pending",
                "severity": "error" if status == "failed" else "warning",
                "message": f"Canonical mutation outbox row is {status}.",
                "details": {
                    "outbox_id": row.get("outbox_id"),
                    "graph_id": row.get("graph_id"),
                    "mutation_lane": row.get("mutation_lane"),
                    "last_error": row.get("last_error"),
                },
            }
        )
    return diagnostics


def _latest_run_end(events: Iterable[AgentEvent]) -> RunEndEvent | None:
    ends = [event for event in events if isinstance(event, RunEndEvent)]
    if not ends:
        return None
    ends.sort(key=lambda event: event.sequence if event.sequence is not None else -1)
    return ends[-1]


def _same_instant(value: Any, iso_text: str) -> bool | None:
    if not isinstance(value, datetime):
        return None
    try:
        expected = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    return value == expected
