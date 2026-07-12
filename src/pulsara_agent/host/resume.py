"""Conversation resume recovery helpers.

V1 resume reopens a durable runtime session in a new HostSession.  It cannot
recover in-process coroutines, terminal managers, or suspended LoopState, so any
durable run that was left ``running`` by a dead host must be terminalized before
the next prompt is rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from pulsara_agent.event import RunEndEvent, RunStartEvent
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.primitives.run_lifecycle import (
    RunStopReason,
    RunTerminalizationKind,
)
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.storage import RUNTIME_TRUTH_SCHEMA_SQL


RESUME_RECOVERED_STOP_REASON = "resume_recovered_interrupted"


@dataclass(frozen=True, slots=True)
class DanglingRunRepairResult:
    runtime_session_id: str
    repaired_run_ids: tuple[str, ...]
    skipped_run_ids: tuple[str, ...]
    projection_rows_updated: int

    @property
    def repaired_count(self) -> int:
        return len(self.repaired_run_ids)


def repair_dangling_runs_for_resume(
    *,
    dsn: str,
    runtime_session_id: str,
    workspace_root: str | None = None,
) -> DanglingRunRepairResult:
    """Append host-teardown ``RUN_END`` events for canonical running runs.

    The repair is intentionally canonical-event-first.  Directly updating
    ``runs.status`` would make inspector summaries look tidy but would not give
    ``rebuild_prior_messages()`` the recovery fact it needs to filter unfinished
    tool calls and explain the interruption to the model.
    """

    _ensure_schema(dsn)
    running = _running_runs_with_latest_context(dsn, runtime_session_id)
    if not running:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=workspace_root)
        return DanglingRunRepairResult(
            runtime_session_id=runtime_session_id,
            repaired_run_ids=(),
            skipped_run_ids=(),
            projection_rows_updated=log.repair_run_projection(),
        )

    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=workspace_root)
    repaired: list[str] = []
    skipped: list[str] = []
    for row in running:
        run_id = str(row["run_id"])
        turn_id = row.get("turn_id")
        reply_id = row.get("reply_id")
        if not isinstance(turn_id, str) or not isinstance(reply_id, str):
            skipped.append(run_id)
            continue
        # Idempotency guard: another resume/open may have repaired it after the
        # initial SELECT but before this append.
        if _run_has_end_event(dsn, runtime_session_id, run_id):
            skipped.append(run_id)
            continue
        starts = [
            event
            for event in log.iter(run_id=run_id)
            if isinstance(event, RunStartEvent)
        ]
        if len(starts) != 1:
            skipped.append(run_id)
            continue
        started = starts[0]
        log.append(
            RunEndEvent(
                id=started.terminal_run_end_event_id,
                run_id=run_id,
                turn_id=turn_id,
                reply_id=reply_id,
                status="aborted",
                stop_reason=RunStopReason.ABORTED,
                terminalization_kind=RunTerminalizationKind.RECOVERED_INTERRUPTED,
                abort_kind=AbortKind.HOST_TEARDOWN.value,
                metadata={
                    "recovered_by": "resume",
                    "resume_stop_reason": RESUME_RECOVERED_STOP_REASON,
                },
            )
        )
        repaired.append(run_id)

    return DanglingRunRepairResult(
        runtime_session_id=runtime_session_id,
        repaired_run_ids=tuple(repaired),
        skipped_run_ids=tuple(skipped),
        projection_rows_updated=log.repair_run_projection(),
    )


def _ensure_schema(dsn: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(RUNTIME_TRUTH_SCHEMA_SQL)


def _running_runs_with_latest_context(dsn: str, runtime_session_id: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select
                    runs.id as run_id,
                    latest.turn_id,
                    latest.reply_id
                from runs
                left join lateral (
                    select turn_id, reply_id
                    from agent_events
                    where session_id = runs.session_id and run_id = runs.id
                    order by sequence desc
                    limit 1
                ) latest on true
                where runs.session_id = %s
                  and runs.status = 'running'
                  and not exists (
                    select 1
                    from agent_events as ended
                    where ended.session_id = runs.session_id
                      and ended.run_id = runs.id
                      and ended.event_type = 'RUN_END'
                  )
                order by runs.started_at asc, runs.id asc
                """,
                (runtime_session_id,),
            )
            return list(cursor.fetchall())


def _run_has_end_event(dsn: str, runtime_session_id: str, run_id: str) -> bool:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select 1
                from agent_events
                where session_id = %s and run_id = %s and event_type = 'RUN_END'
                limit 1
                """,
                (runtime_session_id, run_id),
            )
            return cursor.fetchone() is not None
