"""Protocol-v3 read model over the canonical relational kernel.

Every public read is produced from one read-only repeatable-read transaction.
Occurrence rows select a closed projection branch but never prove canonical
subjects; the subject relation is loaded and validated independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from google.protobuf.message import Message
from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.vocabulary import (
    COMMITTED_EVENT_DESCRIPTORS,
    CommittedEventType,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.storage.migrations.contracts import canonical_json_bytes
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire


MAXIMUM_SNAPSHOT_ENTRIES = STAGE2_LIMITS.snapshot_hard_entries
MAXIMUM_CONTROL_ITEMS = STAGE2_LIMITS.snapshot_hard_control_items
MAXIMUM_OBSERVATION_EVENTS = STAGE2_LIMITS.committed_observation_hard_events
MAXIMUM_OBSERVATION_BYTES = STAGE2_LIMITS.committed_observation_hard_bytes
MAXIMUM_SNAPSHOT_BYTES = STAGE2_LIMITS.snapshot_hard_bytes
MAXIMUM_HISTORY_PAGE_BYTES = STAGE2_LIMITS.history_page_hard_bytes
MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES = STAGE2_LIMITS.tool_argument_display_hard_bytes


class CanonicalProtocolResourceExhausted(RuntimeError):
    """A closed snapshot section cannot be represented within its hard cap."""


class CanonicalProtocolGap(RuntimeError):
    """The requested committed suffix cannot be returned completely."""


@dataclass(frozen=True, slots=True)
class CanonicalObservationBatch:
    through_event_sequence: int
    projections: tuple[wire.CommittedObservationProjection, ...]
    gap_reason: str | None = None


def _enum_name(value: CommittedEventType) -> str:
    result: list[str] = []
    for character in value.value:
        if character.isupper() and result:
            result.append("_")
        result.append(character.upper())
    return "".join(result)


_COMMITTED_ENUM = {
    item.value: getattr(wire, _enum_name(item)) for item in CommittedEventType
}
_ENTRY_TYPES = frozenset(
    {
        CommittedEventType.USER_MESSAGE_ACCEPTED.value,
        CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED.value,
        CommittedEventType.ASSISTANT_TOOL_REQUEST_ACCEPTED.value,
        CommittedEventType.TOOL_RESULT_ACCEPTED.value,
        CommittedEventType.USER_STEER_ACCEPTED.value,
        CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED.value,
    }
)
_EVENT_ONLY_TYPES = frozenset(
    {
        CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED.value,
        CommittedEventType.SUBAGENT_RESULT_ACCEPTED.value,
        CommittedEventType.JOB_ATTEMPT_ACCEPTED.value,
        CommittedEventType.MEMORY_RELATION_ACCEPTED.value,
    }
)
_CONTROL_TYPES = frozenset(_COMMITTED_ENUM) - _ENTRY_TYPES - _EVENT_ONLY_TYPES
COMMITTED_PROJECTION_BRANCH_BY_TYPE: Mapping[str, str] = MappingProxyType(
    {
        event_type: (
            "IMMUTABLE_ENTRY"
            if event_type in _ENTRY_TYPES
            else "EVENT_ONLY"
            if event_type in _EVENT_ONLY_TYPES
            else "CURRENT_CONTROL"
        )
        for event_type in _COMMITTED_ENUM
    }
)

if len(_COMMITTED_ENUM) != 27 or len(COMMITTED_EVENT_DESCRIPTORS) != 27:
    raise RuntimeError(
        "Protocol v3 committed projection map must contain exact 27 types"
    )


class CanonicalProtocolReader:
    def __init__(
        self, connection_provider: VerifiedPostgresConnectionProviderProtocol
    ) -> None:
        self._provider = connection_provider

    def snapshot(
        self,
        *,
        session_id: str,
        maximum_entries: int,
        maximum_control_items: int,
        deadline_monotonic: float,
        maximum_serialized_bytes: int = MAXIMUM_SNAPSHOT_BYTES,
    ) -> wire.CanonicalSessionSnapshot:
        _bounded(maximum_entries, MAXIMUM_SNAPSHOT_ENTRIES, "snapshot entries")
        _bounded(maximum_control_items, MAXIMUM_CONTROL_ITEMS, "control items")
        _bounded_bytes(
            maximum_serialized_bytes, MAXIMUM_SNAPSHOT_BYTES, "snapshot bytes"
        )
        with self._connection(deadline_monotonic) as connection:
            session = self._session(connection, session_id)
            cut = int(session["latest_entry_sequence"])
            rows = connection.execute(
                """
                SELECT * FROM pulsara_v3.transcript_entries
                WHERE session_id = %s AND entry_sequence <= %s
                ORDER BY entry_sequence DESC LIMIT %s
                """,
                (session_id, cut, maximum_entries + 1),
            ).fetchall()
            initial_floor = (
                int(rows[min(maximum_entries, len(rows)) - 1]["entry_sequence"])
                if rows
                else cut + 1
            )
            control = self._control(
                connection,
                session_id=session_id,
                lifecycle=str(session["lifecycle"]),
                maximum_items=maximum_control_items,
                entry_sequence_floor=initial_floor,
            )
            selected_desc: list[wire.CanonicalEntry] = []
            snapshot = self._snapshot_value(
                session=session,
                session_id=session_id,
                cut=cut,
                entries=(),
                control=control,
                has_older=bool(rows),
            )
            if _wire_size(snapshot) > maximum_serialized_bytes:
                raise CanonicalProtocolResourceExhausted(
                    "canonical control cannot fit the snapshot byte bound"
                )
            for row in rows[:maximum_entries]:
                entry = self._entry(connection, row)
                candidate_entries = tuple(reversed((*selected_desc, entry)))
                candidate = self._snapshot_value(
                    session=session,
                    session_id=session_id,
                    cut=cut,
                    entries=candidate_entries,
                    control=control,
                    has_older=len(selected_desc) + 1 < len(rows),
                )
                if _wire_size(candidate) > maximum_serialized_bytes:
                    break
                selected_desc.append(entry)
                snapshot = candidate
            final_floor = (
                snapshot.entries[0].entry_sequence if snapshot.entries else cut + 1
            )
            if final_floor != initial_floor:
                control = self._control(
                    connection,
                    session_id=session_id,
                    lifecycle=str(session["lifecycle"]),
                    maximum_items=maximum_control_items,
                    entry_sequence_floor=final_floor,
                )
                snapshot = self._snapshot_value(
                    session=session,
                    session_id=session_id,
                    cut=cut,
                    entries=tuple(snapshot.entries),
                    control=control,
                    has_older=snapshot.HasField("older_history_cursor"),
                )
                if _wire_size(snapshot) > maximum_serialized_bytes:
                    raise CanonicalProtocolResourceExhausted(
                        "canonical snapshot exceeds its final byte bound"
                    )
            return snapshot

    def history_page(
        self,
        *,
        session_id: str,
        cut_sequence: int,
        before_entry_sequence: int,
        maximum_entries: int,
        deadline_monotonic: float,
        maximum_serialized_bytes: int = MAXIMUM_HISTORY_PAGE_BYTES,
    ) -> tuple[tuple[wire.CanonicalEntry, ...], wire.HistoryCursor | None, bool]:
        _bounded(maximum_entries, MAXIMUM_SNAPSHOT_ENTRIES, "history entries")
        _bounded_bytes(
            maximum_serialized_bytes,
            MAXIMUM_HISTORY_PAGE_BYTES,
            "history page bytes",
        )
        if cut_sequence < 0 or before_entry_sequence < 1:
            raise ValueError("history cursor is invalid")
        with self._connection(deadline_monotonic) as connection:
            session = self._session(connection, session_id)
            if int(session["latest_entry_sequence"]) < cut_sequence:
                raise CanonicalProtocolGap("history cut is ahead of canonical head")
            rows = connection.execute(
                """
                SELECT * FROM pulsara_v3.transcript_entries
                WHERE session_id = %s
                  AND entry_sequence <= %s
                  AND entry_sequence < %s
                ORDER BY entry_sequence DESC LIMIT %s
                """,
                (session_id, cut_sequence, before_entry_sequence, maximum_entries + 1),
            ).fetchall()
            selected_desc: list[wire.CanonicalEntry] = []
            for row in rows[:maximum_entries]:
                entry = self._entry(connection, row)
                candidate = tuple(reversed((*selected_desc, entry)))
                if _entries_wire_size(candidate) > maximum_serialized_bytes:
                    break
                selected_desc.append(entry)
            entries = tuple(reversed(selected_desc))
            if rows and not entries:
                raise CanonicalProtocolResourceExhausted(
                    "one canonical entry exceeds the history page byte bound"
                )
            has_more = len(selected_desc) < len(rows)
            cursor = None
            if has_more and entries:
                cursor = wire.HistoryCursor(
                    session_id=session_id,
                    cut_sequence=cut_sequence,
                    entry_sequence=entries[0].entry_sequence,
                )
            return entries, cursor, has_more

    @staticmethod
    def _snapshot_value(
        *,
        session: Mapping[str, object],
        session_id: str,
        cut: int,
        entries: tuple[wire.CanonicalEntry, ...],
        control: wire.CanonicalControl,
        has_older: bool,
    ) -> wire.CanonicalSessionSnapshot:
        snapshot = wire.CanonicalSessionSnapshot(
            session_id=session_id,
            workspace_id=str(session["workspace_id"]),
            writer_generation=int(session["writer_generation"]),
            entry_sequence_cut=cut,
            event_sequence_cut=int(session["latest_event_sequence"]),
            entries=entries,
            control=control,
        )
        if has_older:
            snapshot.older_history_cursor.CopyFrom(
                wire.HistoryCursor(
                    session_id=session_id,
                    cut_sequence=cut,
                    entry_sequence=(entries[0].entry_sequence if entries else cut + 1),
                )
            )
        snapshot.snapshot_fingerprint = _fingerprint(
            "terminal-canonical-snapshot:v3", snapshot
        )
        return snapshot

    def observe_committed(
        self,
        *,
        session_id: str,
        after_event_sequence: int,
        maximum_events: int,
        maximum_bytes: int,
        deadline_monotonic: float,
    ) -> CanonicalObservationBatch:
        _bounded(maximum_events, MAXIMUM_OBSERVATION_EVENTS, "observation events")
        _bounded(maximum_bytes, MAXIMUM_OBSERVATION_BYTES, "observation bytes")
        if after_event_sequence < 0:
            raise ValueError("event cursor must be non-negative")
        with self._connection(deadline_monotonic) as connection:
            session = self._session(connection, session_id)
            high_water = int(session["latest_event_sequence"])
            if after_event_sequence > high_water:
                return CanonicalObservationBatch(
                    through_event_sequence=high_water,
                    projections=(),
                    gap_reason="CLIENT_CURSOR_AHEAD",
                )
            events = connection.execute(
                """
                SELECT * FROM pulsara_v3.agent_events
                WHERE session_id = %s
                  AND event_sequence > %s AND event_sequence <= %s
                ORDER BY event_sequence LIMIT %s
                """,
                (session_id, after_event_sequence, high_water, maximum_events + 1),
            ).fetchall()
            if len(events) > maximum_events:
                return CanonicalObservationBatch(
                    through_event_sequence=high_water,
                    projections=(),
                    gap_reason="COMMITTED_SUFFIX_EVENT_BOUND",
                )
            control: wire.CanonicalControl | None = None
            result: list[wire.CommittedObservationProjection] = []
            total = 0
            for event in events:
                event_type = str(event["event_type"])
                if event_type not in _COMMITTED_ENUM:
                    return CanonicalObservationBatch(
                        through_event_sequence=high_water,
                        projections=(),
                        gap_reason="COMMITTED_SCHEMA_INCOMPATIBLE",
                    )
                subject_slot, subject_id = _event_subject(event)
                projection = wire.CommittedObservationProjection(
                    event_sequence=int(event["event_sequence"]),
                    event_id=str(event["event_id"]),
                    event_type=_COMMITTED_ENUM[event_type],
                    subject_slot=subject_slot,
                    subject_id=subject_id,
                )
                if event_type in _ENTRY_TYPES:
                    row = connection.execute(
                        """SELECT * FROM pulsara_v3.transcript_entries
                           WHERE session_id = %s AND id = %s""",
                        (session_id, subject_id),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("committed entry subject is missing")
                    projection.projection_kind = wire.IMMUTABLE_ENTRY
                    projection.entry.CopyFrom(self._entry(connection, row))
                elif event_type in _CONTROL_TYPES:
                    if control is None:
                        control = self._control(
                            connection,
                            session_id=session_id,
                            lifecycle=str(session["lifecycle"]),
                            maximum_items=MAXIMUM_CONTROL_ITEMS,
                            entry_sequence_floor=max(
                                1,
                                int(session["latest_entry_sequence"])
                                - MAXIMUM_SNAPSHOT_ENTRIES
                                + 1,
                            ),
                        )
                    projection.projection_kind = wire.CURRENT_CONTROL
                    projection.current_control.CopyFrom(control)
                else:
                    projection.projection_kind = wire.EVENT_ONLY
                total += len(projection.SerializeToString(deterministic=True))
                if total > maximum_bytes:
                    return CanonicalObservationBatch(
                        through_event_sequence=high_water,
                        projections=(),
                        gap_reason="COMMITTED_SUFFIX_BYTE_BOUND",
                    )
                result.append(projection)
            return CanonicalObservationBatch(high_water, tuple(result))

    def resolve_content_reference(
        self,
        *,
        session_id: str,
        entry_id: str,
        block_id: str | None,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        """Re-authorize an exact entry/block content edge before blob hydration."""
        with self._connection(deadline_monotonic) as connection:
            self._session(connection, session_id)
            if block_id:
                row = connection.execute(
                    """
                    SELECT b.inline_content, b.blob_id, b.content_digest,
                           b.content_size, b.content_media_type, b.content_codec
                    FROM pulsara_v3.assistant_message_blocks AS b
                    WHERE b.session_id = %s AND b.assistant_entry_id = %s AND b.id = %s
                    """,
                    (session_id, entry_id, block_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT inline_content, blob_id, content_digest, content_size,
                           content_media_type, content_codec
                    FROM pulsara_v3.transcript_entries
                    WHERE session_id = %s AND id = %s
                    """,
                    (session_id, entry_id),
                ).fetchone()
            if row is None:
                raise KeyError(entry_id if not block_id else block_id)
            return dict(row)

    def _connection(self, deadline_monotonic: float):
        return self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        )

    @staticmethod
    def _session(connection: Any, session_id: str) -> Mapping[str, object]:
        row = connection.execute(
            """
            SELECT id, workspace_id, lifecycle, writer_generation,
                   latest_entry_sequence, latest_event_sequence,
                   latest_prompt_queue_sequence
            FROM pulsara_v3.sessions WHERE id = %s
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row

    def _entries(
        self, connection: Any, rows: Iterable[Mapping[str, object]]
    ) -> tuple[wire.CanonicalEntry, ...]:
        return tuple(self._entry(connection, row) for row in rows)

    def _entry(self, connection: Any, row: Mapping[str, object]) -> wire.CanonicalEntry:
        entry_id = str(row["id"])
        blocks = connection.execute(
            """
            SELECT * FROM pulsara_v3.assistant_message_blocks
            WHERE session_id = %s AND assistant_entry_id = %s
            ORDER BY block_ordinal
            """,
            (row["session_id"], entry_id),
        ).fetchall()
        result = wire.CanonicalEntry(
            entry_id=entry_id,
            turn_id=str(row["turn_id"]),
            entry_sequence=int(row["entry_sequence"]),
            entry_kind=_entry_kind(str(row["entry_kind"])),
            scope_kind=_scope_kind(str(row["conversation_scope_kind"])),
            scope_subagent_task_id=str(row["scope_subagent_task_id"] or ""),
            context_binding_revision_id=str(row["context_binding_revision_id"] or ""),
            provider_input_through_sequence=int(
                row["provider_input_through_sequence"] or 0
            ),
            content=_content_reference(row),
            accepted_at_utc=_utc(row["accepted_at"]),
        )
        for block in blocks:
            arguments = (
                canonical_json_bytes(dict(block["tool_arguments"]))
                if block["tool_arguments"] is not None
                else b""
            )
            item = result.blocks.add(
                block_id=str(block["id"]),
                ordinal=int(block["block_ordinal"]),
                block_kind=str(block["block_kind"]),
                tool_call_id=str(block["tool_call_id"] or ""),
                tool_name=str(block["tool_name"] or ""),
                tool_arguments_preview=arguments[
                    :MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES
                ],
                tool_arguments_truncated=(
                    len(arguments) > MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES
                ),
                tool_arguments_digest=(
                    "sha256:" + sha256(arguments).hexdigest() if arguments else ""
                ),
                tool_arguments_size=len(arguments),
            )
            if block["block_kind"] in ("TEXT", "DATA"):
                item.content.CopyFrom(_content_reference(block))
        return result

    def _control(
        self,
        connection: Any,
        *,
        session_id: str,
        lifecycle: str,
        maximum_items: int,
        entry_sequence_floor: int,
    ) -> wire.CanonicalControl:
        def bounded(sql: str, parameters: tuple[object, ...]) -> tuple[Any, ...]:
            rows = connection.execute(sql, (*parameters, maximum_items + 1)).fetchall()
            if len(rows) > maximum_items:
                raise CanonicalProtocolResourceExhausted(
                    "canonical control section exceeds negotiated limit"
                )
            return tuple(rows)

        turns = bounded(
            """SELECT * FROM pulsara_v3.turns WHERE session_id = %s AND status = 'RUNNING'
               ORDER BY accepted_at, id LIMIT %s""",
            (session_id,),
        )
        queue_total = int(
            connection.execute(
                """SELECT count(*) AS total FROM pulsara_v3.prompt_queue_items
                   WHERE session_id = %s AND status = 'PENDING'""",
                (session_id,),
            ).fetchone()["total"]
        )
        queue = bounded(
            """SELECT * FROM pulsara_v3.prompt_queue_items
               WHERE session_id = %s AND status = 'PENDING'
               ORDER BY queue_sequence, id LIMIT %s""",
            (session_id,),
        )
        attempts = bounded(
            """SELECT a.*, r.result_state, r.result_entry_id
               FROM pulsara_v3.tool_execution_attempts AS a
               JOIN pulsara_v3.transcript_entries AS e
                 ON e.session_id = a.session_id AND e.id = a.assistant_entry_id
               JOIN pulsara_v3.turns AS t
                 ON t.session_id = e.session_id AND t.id = e.turn_id
               LEFT JOIN pulsara_v3.tool_results AS r
                 ON r.session_id = a.session_id AND r.attempt_id = a.id
               WHERE a.session_id = %s AND r.id IS NULL
                 AND (e.entry_sequence >= %s OR t.status = 'RUNNING')
               ORDER BY a.started_at, a.id LIMIT %s""",
            (session_id, entry_sequence_floor),
        )
        tasks = bounded(
            """SELECT t.*, c.id AS result_id,
                      c.entry_id AS result_entry_id,
                      accepted.id AS accepted_root_entry_id
               FROM pulsara_v3.subagent_tasks AS t
               LEFT JOIN pulsara_v3.subagent_task_children AS c
                 ON c.session_id = t.session_id AND c.task_id = t.id
                AND c.child_kind = 'RESULT'
               LEFT JOIN pulsara_v3.transcript_entries AS accepted
                 ON accepted.session_id = c.session_id
                AND accepted.source_subagent_result_id = c.id
               WHERE t.session_id = %s AND (
                 t.status IN ('PENDING', 'ACTIVE') OR
                 (t.status = 'COMPLETED' AND c.id IS NOT NULL AND accepted.id IS NULL)
               )
               ORDER BY t.accepted_at, t.id LIMIT %s""",
            (session_id,),
        )
        jobs = bounded(
            """SELECT j.*, count(a.id)::integer AS attempt_count
               FROM pulsara_v3.durable_jobs AS j
               LEFT JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
               WHERE j.origin_session_id = %s AND j.status IN ('PENDING', 'ACTIVE')
               GROUP BY j.id ORDER BY j.accepted_at, j.id LIMIT %s""",
            (session_id,),
        )
        workspace_id = self._session(connection, session_id)["workspace_id"]
        freshness = connection.execute(
            """SELECT * FROM pulsara_v3.memory_index_state
               WHERE workspace_id = %s ORDER BY channel""",
            (workspace_id,),
        ).fetchall()
        by_channel = {str(item["channel"]): item for item in freshness}
        result = wire.CanonicalControl(
            session_lifecycle=lifecycle,
            prompt_queue_total_count=queue_total,
        )
        for row in turns:
            result.active_turns.add(
                turn_id=str(row["id"]),
                scope_kind=_scope_kind(str(row["conversation_scope_kind"])),
                scope_subagent_task_id=str(row["scope_subagent_task_id"] or ""),
                status=str(row["status"]),
                accepted_at_utc=_utc(row["accepted_at"]),
            )
        for row in queue:
            result.prompt_queue.add(
                queue_item_id=str(row["id"]),
                queue_sequence=int(row["queue_sequence"]),
                status=str(row["status"]),
                delivery_mode=str(row["delivery_mode"]),
                target_turn_id=str(row["target_turn_id"] or ""),
                content=_content_reference(row),
            )
        for row in attempts:
            result.tool_attempts.add(
                attempt_id=str(row["id"]),
                assistant_entry_id=str(row["assistant_entry_id"]),
                tool_call_id=str(row["tool_call_id"]),
                result_state=str(row["result_state"] or ""),
                result_entry_id=str(row["result_entry_id"] or ""),
            )
        for row in tasks:
            result.subagent_tasks.add(
                task_id=str(row["id"]),
                parent_turn_id=str(row["parent_turn_id"] or ""),
                status=str(row["status"]),
                objective=str(row["objective"]),
                result_id=str(row["result_id"] or ""),
                result_entry_id=str(row["result_entry_id"] or ""),
                result_accepted=row["accepted_root_entry_id"] is not None,
            )
        for row in jobs:
            result.jobs.add(
                job_id=str(row["id"]),
                handler_type=str(row["handler_type"]),
                status=str(row["status"]),
                maximum_attempts=int(row["maximum_attempts"]),
                attempt_count=int(row["attempt_count"]),
            )
        for channel in ("FTS", "VECTOR"):
            row = by_channel.get(channel)
            result.memory_freshness.add(
                channel=channel,
                desired_generation=int(row["desired_generation"]) if row else 0,
                applied_generation=int(row["applied_generation"]) if row else 0,
                handler_contract=(
                    f"{row['applied_handler_contract_id']}@{row['applied_handler_contract_version']}"
                    if row
                    else "uninitialized@0"
                ),
            )
        return result


def _bounded(value: int, hard: int, label: str) -> None:
    if not 1 <= value <= hard:
        raise ValueError(f"{label} is outside the Protocol v3 bound")


def _bounded_bytes(value: int, hard: int, label: str) -> None:
    if not 1024 <= value <= hard:
        raise ValueError(f"{label} is outside the Protocol v3 byte bound")


def _wire_size(message: Message) -> int:
    return len(message.SerializeToString(deterministic=True))


def _entries_wire_size(entries: tuple[wire.CanonicalEntry, ...]) -> int:
    # Each repeated-message value has a tag and length prefix.  Eight bytes per
    # item is a conservative upper bound for the fixed Protocol v3 entry cap.
    return sum(_wire_size(entry) + 8 for entry in entries) + 512


def _utc(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _scope_kind(value: str) -> int:
    if value == "ROOT":
        return wire.ROOT
    if value == "SUBAGENT_TASK":
        return wire.SUBAGENT_TASK
    raise RuntimeError("unknown canonical conversation scope")


def _entry_kind(value: str) -> int:
    try:
        return getattr(wire, value)
    except AttributeError as exc:
        raise RuntimeError("unknown canonical entry kind") from exc


def _content_reference(row: Mapping[str, object]) -> wire.CanonicalContentReference:
    inline = row.get("inline_content")
    blob_id = row.get("blob_id")
    digest = str(row.get("content_digest") or "")
    size = int(row.get("content_size") or 0)
    result = wire.CanonicalContentReference(
        kind=wire.INLINE if inline is not None else wire.CANONICAL_BLOB,
        inline_content=bytes(inline or b""),
        digest=digest,
        size=size,
        media_type=str(row.get("content_media_type") or ""),
        codec=str(row.get("content_codec") or ""),
    )
    if result.kind == wire.INLINE:
        if blob_id is not None or len(result.inline_content) != size:
            raise RuntimeError("canonical inline content edge is corrupt")
    elif inline is not None or blob_id is None:
        raise RuntimeError("canonical blob content edge is corrupt")
    return result


def _event_subject(event: Mapping[str, object]) -> tuple[str, str]:
    present = tuple(
        (key, str(event[key]))
        for key in (
            "subject_turn_id",
            "subject_entry_id",
            "subject_tool_attempt_id",
            "subject_job_id",
            "subject_job_attempt_id",
            "subject_queue_item_id",
            "subject_interaction_decision_id",
            "subject_context_binding_revision_id",
            "subject_subagent_task_id",
            "subject_subagent_message_id",
            "subject_subagent_result_id",
            "subject_memory_fact_id",
            "subject_memory_relation_id",
        )
        if event.get(key) is not None
    )
    if len(present) != 1:
        raise RuntimeError("committed event subject union is corrupt")
    return present[0]


def _fingerprint(namespace: str, message: Message) -> str:
    clone = type(message)()
    clone.CopyFrom(message)
    field = clone.DESCRIPTOR.fields_by_name.get("snapshot_fingerprint")
    if field is not None:
        setattr(clone, field.name, "")
    return (
        "sha256:"
        + sha256(
            namespace.encode() + b"\0" + clone.SerializeToString(deterministic=True)
        ).hexdigest()
    )


__all__ = [
    "CanonicalObservationBatch",
    "CanonicalProtocolGap",
    "CanonicalProtocolReader",
    "CanonicalProtocolResourceExhausted",
    "COMMITTED_PROJECTION_BRANCH_BY_TYPE",
    "MAXIMUM_CONTROL_ITEMS",
    "MAXIMUM_OBSERVATION_BYTES",
    "MAXIMUM_OBSERVATION_EVENTS",
    "MAXIMUM_HISTORY_PAGE_BYTES",
    "MAXIMUM_SNAPSHOT_BYTES",
    "MAXIMUM_SNAPSHOT_ENTRIES",
    "MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES",
]
