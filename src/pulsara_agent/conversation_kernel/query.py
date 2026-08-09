"""Bounded canonical conversation query ports for reopen and Inspector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Mapping

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.limits import (
    STAGE2_LIMITS,
    STAGE2_STRUCTURAL_BUDGETS,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class CanonicalConversationPage:
    session_id: str
    workspace_id: str
    lifecycle: str
    writer_generation: int
    through_entry_sequence: int
    through_event_sequence: int
    after_entry_sequence: int
    entries: tuple[Mapping[str, object], ...]
    has_more: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "lifecycle": self.lifecycle,
            "writer_generation": self.writer_generation,
            "through_entry_sequence": self.through_entry_sequence,
            "through_event_sequence": self.through_event_sequence,
            "after_entry_sequence": self.after_entry_sequence,
            "entries": [_public_row(item) for item in self.entries],
            "has_more": self.has_more,
        }


@dataclass(frozen=True, slots=True)
class CanonicalInspectorView:
    conversation: CanonicalConversationPage
    turns: tuple[Mapping[str, object], ...]
    tool_attempts: tuple[Mapping[str, object], ...]
    prompt_queue: tuple[Mapping[str, object], ...]
    subagent_tasks: tuple[Mapping[str, object], ...]
    jobs: tuple[Mapping[str, object], ...]
    selective_events: tuple[Mapping[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "inspect_kind": "canonical_conversation_kernel.v3",
            "conversation": self.conversation.to_dict(),
            "turns": [_public_row(item) for item in self.turns],
            "tool_attempts": [_public_row(item) for item in self.tool_attempts],
            "prompt_queue": [_public_row(item) for item in self.prompt_queue],
            "subagent_tasks": [_public_row(item) for item in self.subagent_tasks],
            "jobs": [_public_row(item) for item in self.jobs],
            "selective_events": [_public_row(item) for item in self.selective_events],
        }


class CanonicalConversationQuery:
    def __init__(
        self, connection_provider: VerifiedPostgresConnectionProviderProtocol
    ) -> None:
        self._provider = connection_provider

    def page_entries(
        self,
        *,
        session_id: str,
        after_entry_sequence: int = 0,
        maximum_entries: int = 256,
        deadline_monotonic: float,
    ) -> CanonicalConversationPage:
        if after_entry_sequence < 0 or not 1 <= maximum_entries <= 1024:
            raise ValueError("canonical page request is out of bounds")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return self._page_entries_on_connection(
                connection,
                session_id=session_id,
                after_entry_sequence=after_entry_sequence,
                maximum_entries=maximum_entries,
            )

    @staticmethod
    def _page_entries_on_connection(
        connection: Any,
        *,
        session_id: str,
        after_entry_sequence: int,
        maximum_entries: int,
    ) -> CanonicalConversationPage:
        """Read one conversation page from the caller's exact MVCC cut."""
        session = connection.execute(
            """
                SELECT id, workspace_id, lifecycle, writer_generation,
                       latest_entry_sequence, latest_event_sequence
                FROM pulsara_v3.sessions WHERE id = %s
                """,
            (session_id,),
        ).fetchone()
        if session is None:
            raise KeyError(session_id)
        through = int(session["latest_entry_sequence"])
        rows = connection.execute(
            """
                SELECT e.id, e.turn_id, e.entry_sequence, e.entry_kind,
                       e.conversation_scope_kind, e.scope_subagent_task_id,
                       e.context_binding_revision_id,
                       e.provider_input_through_sequence,
                       e.source_job_id, e.source_subagent_result_id,
                       e.content_digest, e.content_size, e.content_media_type,
                       e.content_codec, e.accepted_at,
                       COALESCE(
                         jsonb_agg(
                           jsonb_build_object(
                             'id', b.id,
                             'ordinal', b.block_ordinal,
                             'kind', b.block_kind,
                             'tool_call_id', b.tool_call_id,
                             'tool_name', b.tool_name,
                             'tool_arguments', b.tool_arguments,
                             'content_digest', b.content_digest,
                             'content_size', b.content_size
                           ) ORDER BY b.block_ordinal
                         ) FILTER (WHERE b.id IS NOT NULL),
                         '[]'::jsonb
                       ) AS blocks
                FROM pulsara_v3.transcript_entries AS e
                LEFT JOIN pulsara_v3.assistant_message_blocks AS b
                  ON b.session_id = e.session_id AND b.assistant_entry_id = e.id
                WHERE e.session_id = %s
                  AND e.entry_sequence > %s AND e.entry_sequence <= %s
                GROUP BY e.id
                ORDER BY e.entry_sequence
                LIMIT %s
                """,
            (session_id, after_entry_sequence, through, maximum_entries + 1),
        ).fetchall()
        has_more = len(rows) > maximum_entries
        return CanonicalConversationPage(
            session_id=session_id,
            workspace_id=str(session["workspace_id"]),
            lifecycle=str(session["lifecycle"]),
            writer_generation=int(session["writer_generation"]),
            through_entry_sequence=through,
            through_event_sequence=int(session["latest_event_sequence"]),
            after_entry_sequence=after_entry_sequence,
            entries=tuple(dict(row) for row in rows[:maximum_entries]),
            has_more=has_more,
        )

    def inspect(
        self,
        *,
        session_id: str,
        maximum_entries: int = 256,
        maximum_events: int = 256,
        deadline_monotonic: float,
    ) -> CanonicalInspectorView:
        if not 1 <= maximum_events <= 1024:
            raise ValueError("selective event page is out of bounds")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            conversation = self._page_entries_on_connection(
                connection,
                session_id=session_id,
                after_entry_sequence=0,
                maximum_entries=maximum_entries,
            )

            # These rows are queried directly.  No occurrence event is joined
            # to prove a canonical aggregate.
            def rows(sql: str) -> tuple[Mapping[str, object], ...]:
                return tuple(
                    dict(item)
                    for item in connection.execute(sql, (session_id,)).fetchall()
                )

            turns = rows(
                """SELECT * FROM pulsara_v3.turns
                   WHERE session_id = %s ORDER BY accepted_at, id"""
            )
            attempts = rows(
                """SELECT a.*, r.result_state, r.result_entry_id
                   FROM pulsara_v3.tool_execution_attempts AS a
                   LEFT JOIN pulsara_v3.tool_results AS r
                     ON r.session_id = a.session_id AND r.attempt_id = a.id
                   WHERE a.session_id = %s ORDER BY a.started_at, a.id"""
            )
            queue = rows(
                """SELECT * FROM pulsara_v3.prompt_queue_items
                   WHERE session_id = %s ORDER BY queue_sequence, id"""
            )
            tasks = rows(
                """SELECT * FROM pulsara_v3.subagent_tasks
                   WHERE session_id = %s ORDER BY accepted_at, id"""
            )
            jobs = rows(
                """SELECT * FROM pulsara_v3.durable_jobs
                   WHERE origin_session_id = %s ORDER BY accepted_at, id"""
            )
            events = tuple(
                dict(item)
                for item in connection.execute(
                    """
                    SELECT * FROM pulsara_v3.agent_events
                    WHERE session_id = %s
                    ORDER BY event_sequence DESC
                    LIMIT %s
                    """,
                    (session_id, maximum_events),
                ).fetchall()[::-1]
            )
        return CanonicalInspectorView(
            conversation=conversation,
            turns=turns,
            tool_attempts=attempts,
            prompt_queue=queue,
            subagent_tasks=tasks,
            jobs=jobs,
            selective_events=events,
        )

    def inspect_memory(
        self,
        *,
        memory_id: str,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            fact = connection.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE id = %s",
                (memory_id,),
            ).fetchone()
            if fact is None:
                raise KeyError(memory_id)
            relations = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_relations
                WHERE workspace_id = %s
                  AND (source_fact_id = %s OR target_fact_id = %s)
                ORDER BY accepted_at, id LIMIT 256
                """,
                (fact["workspace_id"], memory_id, memory_id),
            ).fetchall()
            freshness = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s ORDER BY channel
                """,
                (fact["workspace_id"],),
            ).fetchall()
        return {
            "inspect_kind": "canonical_memory.v3",
            "fact": _public_row(fact),
            "relations": [_public_row(item) for item in relations],
            "index_freshness": [_public_row(item) for item in freshness],
        }

    def inspect_health(self, *, deadline_monotonic: float) -> Mapping[str, object]:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:

            def grouped(relation: str, column: str) -> list[dict[str, object]]:
                return [
                    _public_row(item)
                    for item in connection.execute(
                        f"""SELECT {column} AS state, count(*)::bigint AS count
                            FROM pulsara_v3.{relation}
                            GROUP BY {column} ORDER BY {column}"""
                    ).fetchall()
                ]

            sessions = grouped("sessions", "lifecycle")
            jobs = grouped("durable_jobs", "status")
            queue = grouped("prompt_queue_items", "status")
            task = grouped("subagent_tasks", "status")
            freshness = connection.execute(
                """
                SELECT channel,
                       count(*) FILTER (WHERE applied_generation = desired_generation)::bigint
                         AS complete,
                       count(*) FILTER (WHERE applied_generation < desired_generation)::bigint
                         AS partial,
                       max(desired_generation - applied_generation)::bigint AS maximum_lag
                FROM pulsara_v3.memory_index_state GROUP BY channel ORDER BY channel
                """
            ).fetchall()
        return {
            "inspect_kind": "canonical_kernel_health.v3",
            "conversation_authority": "pulsara_v3",
            "protocol_major": 3,
            "sessions": sessions,
            "jobs": jobs,
            "prompt_queue": queue,
            "subagent_tasks": task,
            "memory_index": [_public_row(item) for item in freshness],
            "runtime_limit_contract": "stage2_runtime_limits.v1",
            "runtime_limits": asdict(STAGE2_LIMITS),
            "structural_budget_contract": "stage2_structural_budgets.v1",
            "structural_budgets": asdict(STAGE2_STRUCTURAL_BUDGETS),
            "legacy_event_replay": False,
            "oxigraph_enabled": False,
        }


def _public_row(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _public_value(item) for key, item in value.items()}


def _public_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {
            "byte_length": len(value),
            "sha256": sha256(value).hexdigest(),
        }
    if isinstance(value, Mapping):
        return _public_row(value)
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    return value


__all__ = [
    "CanonicalConversationPage",
    "CanonicalConversationQuery",
    "CanonicalInspectorView",
]
