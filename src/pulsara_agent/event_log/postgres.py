"""PostgreSQL-backed EventLog implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event.events import AgentEvent, ReplyStartEvent, RunEndEvent, RunStartEvent
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.message.message import AssistantMsg, Msg
from pulsara_agent.message.reducer import MessageReducer


@dataclass(slots=True)
class PostgresEventLog:
    dsn: str
    runtime_session_id: str
    workspace_root: str | Path | None = None

    def append(self, event: AgentEvent) -> AgentEvent:
        return self.extend([event])[0]

    def extend(self, events: Iterable[AgentEvent]) -> list[AgentEvent]:
        event_list = list(events)
        if not event_list:
            return []

        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                self._lock_session(cursor)
                stored_events: list[AgentEvent] = []
                next_sequence = self._next_sequence(cursor)
                for event in event_list:
                    self._ensure_parent_rows(cursor, event)
                    stored, next_sequence = self._with_canonical_sequence(event, next_sequence)
                    self._insert_event(cursor, stored)
                    self._sync_run_projection(cursor, stored)
                    stored_events.append(stored)
                return stored_events

    def repair_run_projection(self) -> int:
        """Rebuild this session's runs summary rows from canonical events."""

        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                self._lock_session(cursor)
                cursor.execute(
                    """
                    with starts as (
                        select run_id, min(created_at) as started_at
                        from agent_events
                        where session_id = %s and event_type = 'RUN_START'
                        group by run_id
                    )
                    update runs r
                    set started_at = starts.started_at
                    from starts
                    where r.session_id = %s and r.id = starts.run_id
                    """,
                    (self.runtime_session_id, self.runtime_session_id),
                )
                updated = cursor.rowcount
                cursor.execute(
                    """
                    with latest_end as (
                        select distinct on (run_id)
                            run_id,
                            payload->>'status' as status,
                            payload->>'stop_reason' as stop_reason,
                            created_at as completed_at
                        from agent_events
                        where session_id = %s and event_type = 'RUN_END'
                        order by run_id, sequence desc
                    )
                    update runs r
                    set
                        status = latest_end.status,
                        stop_reason = latest_end.stop_reason,
                        completed_at = latest_end.completed_at
                    from latest_end
                    where r.session_id = %s and r.id = latest_end.run_id
                    """,
                    (self.runtime_session_id, self.runtime_session_id),
                )
                updated += cursor.rowcount
                cursor.execute(
                    """
                    update runs r
                    set status = 'running', stop_reason = null, completed_at = null
                    where r.session_id = %s
                      and not exists (
                        select 1
                        from agent_events e
                        where e.session_id = %s
                          and e.run_id = r.id
                          and e.event_type = 'RUN_END'
                      )
                    """,
                    (self.runtime_session_id, self.runtime_session_id),
                )
                updated += cursor.rowcount
                return updated

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]:
        predicates = [sql.SQL("session_id = %s")]
        params: list[str] = [self.runtime_session_id]

        if after_sequence is not None:
            predicates.append(sql.SQL("sequence > %s"))
            params.append(after_sequence)
        if run_id is not None:
            predicates.append(sql.SQL("run_id = %s"))
            params.append(run_id)
        if turn_id is not None:
            predicates.append(sql.SQL("turn_id = %s"))
            params.append(turn_id)
        if reply_id is not None:
            predicates.append(sql.SQL("reply_id = %s"))
            params.append(reply_id)

        query = sql.SQL(
            """
            select payload
            from agent_events
            where {where}
            order by sequence asc
            """
        ).format(where=sql.SQL(" and ").join(predicates))

        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return [load_agent_event(row["payload"]) for row in cursor.fetchall()]

    def replay(self, reply_id: str) -> Msg:
        events = self.iter(reply_id=reply_id)
        start = next((event for event in events if isinstance(event, ReplyStartEvent)), None)
        message = AssistantMsg(
            id=reply_id,
            name=start.name if start else "assistant",
            content=[],
            created_at=start.created_at if start else None,
        )
        reducer = MessageReducer(message)
        for event in events:
            reducer.append(event)
        return reducer.message

    def _lock_session(self, cursor) -> None:
        cursor.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (self.runtime_session_id,),
        )

    def _ensure_parent_rows(self, cursor, event: AgentEvent) -> None:
        cursor.execute(
            """
            insert into sessions (id, workspace_root)
            values (%s, %s)
            on conflict (id) do nothing
            """,
            (self.runtime_session_id, str(self.workspace_root) if self.workspace_root is not None else None),
        )
        cursor.execute(
            """
            insert into runs (id, session_id)
            values (%s, %s)
            on conflict (id) do nothing
            """,
            (event.run_id, self.runtime_session_id),
        )
        self._ensure_run_belongs_to_session(cursor, event)
        cursor.execute(
            """
            insert into turns (id, session_id, run_id, turn_index)
            select %s, %s, %s, coalesce(max(turn_index), 0) + 1
            from turns
            where run_id = %s
            on conflict (id) do nothing
            """,
            (event.turn_id, self.runtime_session_id, event.run_id, event.run_id),
        )
        self._ensure_turn_belongs_to_run(cursor, event)

    def _ensure_run_belongs_to_session(self, cursor, event: AgentEvent) -> None:
        cursor.execute("select session_id from runs where id = %s", (event.run_id,))
        row = cursor.fetchone()
        if row is None:
            return
        session_id = row[0]
        if session_id != self.runtime_session_id:
            raise ValueError(
                f"run_id {event.run_id!r} already belongs to runtime session {session_id!r}"
            )

    def _ensure_turn_belongs_to_run(self, cursor, event: AgentEvent) -> None:
        cursor.execute("select session_id, run_id from turns where id = %s", (event.turn_id,))
        row = cursor.fetchone()
        if row is None:
            return
        session_id, run_id = row
        if session_id != self.runtime_session_id or run_id != event.run_id:
            raise ValueError(
                f"turn_id {event.turn_id!r} already belongs to runtime session {session_id!r} "
                f"and run {run_id!r}"
            )

    def _insert_event(self, cursor, stored: AgentEvent) -> None:
        payload = dump_agent_event(stored)
        cursor.execute(
            """
            insert into agent_events (
                id,
                session_id,
                run_id,
                turn_id,
                reply_id,
                sequence,
                event_type,
                created_at,
                payload
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s)
            """,
            (
                stored.id,
                self.runtime_session_id,
                stored.run_id,
                stored.turn_id,
                stored.reply_id,
                stored.sequence,
                str(stored.type),
                stored.created_at,
                Jsonb(payload),
            ),
        )

    def _sync_run_projection(self, cursor, stored: AgentEvent) -> None:
        if isinstance(stored, RunStartEvent):
            cursor.execute(
                """
                update runs
                set status = 'running',
                    stop_reason = null,
                    started_at = %s::timestamptz,
                    completed_at = null
                where id = %s and session_id = %s
                """,
                (stored.created_at, stored.run_id, self.runtime_session_id),
            )
            return

        if isinstance(stored, RunEndEvent):
            cursor.execute(
                """
                update runs
                set status = %s,
                    stop_reason = %s,
                    completed_at = %s::timestamptz
                where id = %s and session_id = %s
                """,
                (stored.status, stored.stop_reason, stored.created_at, stored.run_id, self.runtime_session_id),
            )

    def _next_sequence(self, cursor) -> int:
        cursor.execute(
            """
            select coalesce(max(sequence), 0) + 1
            from agent_events
            where session_id = %s
            """,
            (self.runtime_session_id,),
        )
        return cursor.fetchone()[0]

    def _with_canonical_sequence(self, event: AgentEvent, next_sequence: int) -> tuple[AgentEvent, int]:
        if event.sequence is not None:
            return event, max(next_sequence, event.sequence + 1)

        return event.model_copy(update={"sequence": next_sequence}), next_sequence + 1
