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
from pulsara_agent.llm.provider_replay import (
    ProviderAssistantReplayCodecKind,
    ProviderVisibleReasoningBlock,
    project_provider_visible_reasoning,
)
from pulsara_agent.ports.live_agent_event import ReasoningPresentationKind
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    freeze_json,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanInteractionBinding,
    extract_plan_draft,
    extract_plan_question,
)
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
MAXIMUM_REASONING_INLINE_BYTES = STAGE2_LIMITS.inline_content_hard_bytes


class CanonicalProtocolResourceExhausted(RuntimeError):
    """A closed snapshot section cannot be represented within its hard cap."""


class CanonicalProtocolGap(RuntimeError):
    """The requested committed suffix cannot be returned completely."""


class CanonicalQueueContentNotPending(KeyError):
    """An exact queue item exists, but no longer authorizes pending-body reads."""

    def __init__(
        self,
        *,
        queue_item_id: str,
        status: str,
        consumed_entry_id: str | None,
    ) -> None:
        super().__init__(queue_item_id)
        self.queue_item_id = queue_item_id
        self.status = status
        self.consumed_entry_id = consumed_entry_id


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
        CommittedEventType.USER_CONTROL_FEEDBACK_ACCEPTED.value,
        CommittedEventType.PLAN_CONTINUATION_ACCEPTED.value,
        CommittedEventType.INTER_AGENT_MESSAGE_ACCEPTED.value,
    }
)
_EVENT_ONLY_TYPES = frozenset(
    {
        CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED.value,
        CommittedEventType.SUBAGENT_RESULT_ACCEPTED.value,
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

if len(_COMMITTED_ENUM) != 30 or len(COMMITTED_EVENT_DESCRIPTORS) != 30:
    raise RuntimeError(
        "Protocol v3 committed projection map must contain exact 30 types"
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

    def subagent_activity_page(
        self,
        *,
        session_id: str,
        task_id: str,
        after_entry_sequence: int,
        maximum_entries: int,
        deadline_monotonic: float,
    ) -> tuple[tuple[wire.CanonicalEntry, ...], bool]:
        """Read exact canonical activity metadata for one durable subagent task."""

        _bounded(maximum_entries, MAXIMUM_SNAPSHOT_ENTRIES, "task activity entries")
        if not task_id or after_entry_sequence < 0:
            raise ValueError("task activity cursor is invalid")
        with self._connection(deadline_monotonic) as connection:
            task = connection.execute(
                "SELECT id FROM pulsara_v3.subagent_tasks WHERE session_id = %s AND id = %s",
                (session_id, task_id),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            rows = connection.execute(
                """SELECT * FROM pulsara_v3.transcript_entries
                   WHERE session_id = %s
                     AND conversation_scope_kind = 'SUBAGENT_TASK'
                     AND scope_subagent_task_id = %s
                     AND entry_sequence > %s
                   ORDER BY entry_sequence, id LIMIT %s""",
                (session_id, task_id, after_entry_sequence, maximum_entries + 1),
            ).fetchall()
            return (
                tuple(self._entry(connection, row) for row in rows[:maximum_entries]),
                len(rows) > maximum_entries,
            )

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
        deadline_monotonic: float,
        entry_id: str | None = None,
        queue_item_id: str | None = None,
        block_id: str | None = None,
        image_ref_ordinal: int | None = None,
    ) -> Mapping[str, object]:
        """Re-authorize an exact entry/block content edge before blob hydration."""
        if (entry_id is None) == (queue_item_id is None):
            raise ValueError("exactly one content target is required")
        if image_ref_ordinal is not None and (
            isinstance(image_ref_ordinal, bool) or image_ref_ordinal < 0
        ):
            raise ValueError("prompt image ordinal is invalid")
        if queue_item_id is not None and block_id is not None:
            raise ValueError("queue content has no block target")
        if image_ref_ordinal is not None and block_id is not None:
            raise ValueError("prompt image content has no block target")
        with self._connection(deadline_monotonic) as connection:
            self._session(connection, session_id)
            if queue_item_id is not None:
                row = connection.execute(
                    """
                    SELECT session_id, workspace_id, inline_content, blob_id,
                           content_digest, content_size, content_media_type,
                           content_codec, status,
                           consumed_entry_id
                    FROM pulsara_v3.prompt_queue_items
                    WHERE session_id = %s AND id = %s
                    """,
                    (session_id, queue_item_id),
                ).fetchone()
                if row is not None and str(row["status"]) != "PENDING":
                    raise CanonicalQueueContentNotPending(
                        queue_item_id=queue_item_id,
                        status=str(row["status"]),
                        consumed_entry_id=(
                            str(row["consumed_entry_id"])
                            if row["consumed_entry_id"] is not None
                            else None
                        ),
                    )
            elif block_id:
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
                    SELECT session_id, workspace_id, entry_kind, inline_content,
                           blob_id, content_digest, content_size,
                           content_media_type, content_codec
                    FROM pulsara_v3.transcript_entries
                    WHERE session_id = %s AND id = %s
                    """,
                    (session_id, entry_id),
                ).fetchone()
            if row is None:
                if block_id:
                    derived = self._resolve_reasoning_content(
                        connection,
                        session_id=session_id,
                        entry_id=entry_id,
                        block_id=block_id,
                    )
                    if derived is not None:
                        return derived
                raise KeyError(entry_id if not block_id else block_id)
            if image_ref_ordinal is not None:
                if (
                    queue_item_id is None
                    and str(row["entry_kind"]) not in {"USER_MESSAGE", "USER_STEER"}
                ):
                    raise ValueError("only user prompt entries have image occurrences")
                from pulsara_agent.conversation_kernel.prompt_storage import (
                    resolve_canonical_prompt_image_reference,
                )

                return resolve_canonical_prompt_image_reference(
                    connection,
                    row=row,
                    ref_ordinal=image_ref_ordinal,
                    queue_item_id=queue_item_id,
                    transcript_entry_id=entry_id,
                )
            return dict(row)

    def resolve_tool_artifact_reference(
        self,
        *,
        session_id: str,
        result_entry_id: str,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        """Resolve one browser-visible result entry to its canonical artifact edge."""
        with self._connection(deadline_monotonic) as connection:
            self._session(connection, session_id)
            row = connection.execute(
                """
                SELECT workspace_id, output_artifact_id,
                       output_artifact_disposition, output_source_coverage,
                       output_display_kind, output_source_coverage_reason,
                       output_artifact_unavailability_reason
                FROM pulsara_v3.tool_results
                WHERE session_id = %s AND result_entry_id = %s
                """,
                (session_id, result_entry_id),
            ).fetchone()
            if row is None:
                raise KeyError(result_entry_id)
            return dict(row)

    def _resolve_reasoning_content(
        self,
        connection: Any,
        *,
        session_id: str,
        entry_id: str,
        block_id: str,
    ) -> Mapping[str, object] | None:
        prefix = f"{entry_id}:provider-reasoning:"
        if not block_id.startswith(prefix):
            return None
        raw_ordinal = block_id.removeprefix(prefix)
        if not raw_ordinal.isdigit() or str(int(raw_ordinal)) != raw_ordinal:
            return None
        entry = connection.execute(
            """
            SELECT * FROM pulsara_v3.transcript_entries
            WHERE session_id = %s AND id = %s
            """,
            (session_id, entry_id),
        ).fetchone()
        if entry is None:
            return None
        reasoning = self._reasoning_blocks(connection, entry)
        ordinal = int(raw_ordinal)
        if ordinal >= len(reasoning):
            return None
        content = reasoning[ordinal].text.encode("utf-8")
        return {
            "inline_content": content,
            "blob_id": None,
            "content_digest": "sha256:" + sha256(content).hexdigest(),
            "content_size": len(content),
            "content_media_type": "text/plain",
            "content_codec": "utf-8",
        }

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
        from pulsara_agent.conversation_kernel.fork_history import read_fork_anchor
        from pulsara_agent.conversation_kernel.reader import historical_source_attribution
        stored_row = row
        row = historical_source_attribution(row)
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
            turn_id=str(row["turn_id"] if row["entry_owner_kind"] == "EXECUTED_TURN" else row["imported_history_group_id"]),
            entry_owner_kind=str(row["entry_owner_kind"]),
            fork_eligible=(
                row["entry_kind"] == "ASSISTANT_MESSAGE"
                and read_fork_anchor(connection, str(row["session_id"]), entry_id) is not None
            ),
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
            source_subagent_task_id=str(row["source_subagent_task_id"] or ""),
        )
        if row["entry_kind"] == "TOOL_RESULT":
            tool_result = connection.execute(
                """SELECT tool_call_entry_id, tool_call_id, result_state,
                          output_artifact_disposition, output_source_coverage,
                          output_display_kind, output_source_coverage_reason,
                          output_artifact_unavailability_reason
                   FROM pulsara_v3.tool_results
                   WHERE session_id=%s AND result_entry_id=%s""",
                (row["session_id"], entry_id),
            ).fetchone()
            if tool_result is None:
                raise ValueError("canonical tool result is missing its relational row")
            result.tool_result.CopyFrom(wire.CanonicalToolResult(
                assistant_entry_id=str(tool_result["tool_call_entry_id"]),
                tool_call_id=str(tool_result["tool_call_id"]),
                result_state=str(tool_result["result_state"]),
                artifact_disposition=str(tool_result["output_artifact_disposition"]),
                source_coverage=str(tool_result["output_source_coverage"]),
                display_kind=str(tool_result["output_display_kind"]),
                source_coverage_reason=str(tool_result["output_source_coverage_reason"] or ""),
                artifact_unavailability_reason=str(tool_result["output_artifact_unavailability_reason"] or ""),
            ))
        if (
            row["entry_owner_kind"] == "EXECUTED_TURN"
            and row["entry_kind"] in {"USER_MESSAGE", "USER_STEER"}
        ):
            source = connection.execute(
                """
                SELECT id, command_id, delivery_mode
                FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND consumed_entry_id = %s
                """,
                (row["session_id"], entry_id),
            ).fetchone()
            if source is not None:
                result.input_source.CopyFrom(wire.CanonicalInputSource(
                    queue_item_id=str(source["id"]),
                    command_id=str(source["command_id"]),
                    delivery_mode=str(source["delivery_mode"]),
                ))
        for ordinal, reasoning in enumerate(self._reasoning_blocks(connection, stored_row)):
            content = reasoning.text.encode("utf-8")
            target = result.reasoning_blocks.add(
                block_id=f"{entry_id}:provider-reasoning:{ordinal}",
                ordinal=ordinal,
                presentation_kind=(
                    wire.REASONING_PRESENTATION_SUMMARY
                    if reasoning.presentation_kind is ReasoningPresentationKind.SUMMARY
                    else wire.REASONING_PRESENTATION_FULL
                ),
            )
            target.content.CopyFrom(_reasoning_content_reference(content))
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
                tool_arguments_preview=arguments[:MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES],
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

    @staticmethod
    def _reasoning_blocks(
        connection: Any,
        entry: Mapping[str, object],
    ) -> tuple[ProviderVisibleReasoningBlock, ...]:
        entry_kind = str(entry["entry_kind"])
        if entry_kind not in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"}:
            return ()
        disposition = str(entry.get("provider_replay_disposition") or "")
        replay_id = entry.get("provider_replay_fragment_id")
        if disposition == "PUBLIC_SEMANTIC_ONLY":
            if replay_id is not None:
                raise RuntimeError("public-only assistant has a replay pointer")
            return ()
        if disposition != "NATIVE_REPLAY" or replay_id is None:
            raise RuntimeError("assistant provider replay union is invalid")
        row = connection.execute(
            """
            SELECT codec_kind, payload_bytes, payload_digest, payload_size, item_count
            FROM pulsara_v3.provider_assistant_replay_fragments
            WHERE session_id = %s AND assistant_entry_id = %s AND id = %s
            """,
            (entry["session_id"], entry["id"], replay_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("assistant provider replay row is missing")
        try:
            return project_provider_visible_reasoning(
                codec_kind=ProviderAssistantReplayCodecKind(str(row["codec_kind"])),
                payload_bytes=bytes(row["payload_bytes"]),
                expected_payload_digest=str(row["payload_digest"]),
                expected_payload_size=int(row["payload_size"]),
                expected_item_count=int(row["item_count"]),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("assistant provider replay body is corrupt") from exc

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
                 ON e.entry_owner_kind = 'EXECUTED_TURN' AND e.session_id = a.session_id AND e.id = a.assistant_entry_id
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
                      c.entry_id AS result_entry_id, c.result_source,
                      c.summary AS result_summary,
                      coalesce(deps.ids, ARRAY[]::text[]) AS dependency_task_ids,
                      accepted.id AS accepted_root_entry_id
               FROM pulsara_v3.subagent_tasks AS t
               LEFT JOIN pulsara_v3.subagent_task_children AS c
                 ON c.session_id = t.session_id AND c.task_id = t.id
                AND c.child_kind = 'RESULT'
               LEFT JOIN pulsara_v3.transcript_entries AS accepted
                 ON accepted.entry_owner_kind = 'EXECUTED_TURN' AND accepted.session_id = t.session_id
                AND accepted.source_subagent_task_id = t.id
               LEFT JOIN LATERAL (
                 SELECT array_agg(edge.dependency_task_id
                                  ORDER BY edge.dependency_ordinal) AS ids
                 FROM pulsara_v3.subagent_task_dependencies AS edge
                 WHERE edge.session_id = t.session_id AND edge.task_id = t.id
               ) AS deps ON TRUE
               WHERE t.session_id = %s AND (
                 t.status IN ('PENDING_START', 'WAITING_DEPENDENCY', 'ACTIVE') OR
                 (t.status IN (
                    'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED',
                    'BLOCKED_DEPENDENCY_FAILED'
                  ) AND accepted.id IS NULL)
               )
               ORDER BY t.accepted_at, t.id LIMIT %s""",
            (session_id,),
        )
        active_plan = connection.execute(
            """
            SELECT * FROM pulsara_v3.plan_workflows
            WHERE session_id = %s AND status = 'ACTIVE'
            """,
            (session_id,),
        ).fetchone()
        open_plan = connection.execute(
            """
            SELECT i.*, b.tool_arguments
            FROM pulsara_v3.plan_interactions AS i
            JOIN pulsara_v3.assistant_message_blocks AS b
              ON b.session_id = i.session_id
             AND b.assistant_entry_id = i.assistant_entry_id
             AND b.tool_call_id = i.tool_call_id
            WHERE i.session_id = %s AND i.status = 'OPEN'
            """,
            (session_id,),
        ).fetchone()
        latest_handoff = connection.execute(
            """
            SELECT w.*,
                   i.id AS interaction_id,
                   ce.id AS claim_entry_id,
                   cq.id AS claim_queue_item_id,
                   EXISTS (
                       SELECT 1 FROM pulsara_v3.plan_workflows AS newer
                       WHERE newer.session_id = w.session_id
                         AND newer.workflow_ordinal > w.workflow_ordinal
                   ) AS superseded
            FROM pulsara_v3.plan_workflows AS w
            LEFT JOIN LATERAL (
                SELECT id FROM pulsara_v3.plan_interactions
                WHERE session_id = w.session_id
                  AND plan_workflow_id = w.id
                  AND status IN ('CANCELLED', 'ABORTED')
                ORDER BY interaction_ordinal DESC LIMIT 1
            ) AS i ON TRUE
            LEFT JOIN LATERAL (
                SELECT id FROM pulsara_v3.transcript_entries
                WHERE session_id = w.session_id
                  AND entry_owner_kind = 'EXECUTED_TURN'
                  AND source_plan_workflow_id = w.id
                  AND source_plan_handoff_kind IN (
                      'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
                  )
                ORDER BY entry_sequence LIMIT 1
            ) AS ce ON TRUE
            LEFT JOIN LATERAL (
                SELECT id FROM pulsara_v3.prompt_queue_items
                WHERE session_id = w.session_id
                  AND pending_plan_handoff_workflow_id = w.id
                ORDER BY queue_sequence LIMIT 1
            ) AS cq ON TRUE
            WHERE w.session_id = %s
              AND w.status IN ('CANCELLED', 'FORCE_EXITED')
            ORDER BY w.workflow_ordinal DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        latest_context_compaction = connection.execute(
            """
            SELECT revision.turn_id,
                   revision.id AS context_binding_revision_id,
                   revision.source_through_sequence,
                   event.accepted_at,
                   coalesce((
                       SELECT max(entry.entry_sequence)
                       FROM pulsara_v3.agent_events AS prior
                       JOIN pulsara_v3.transcript_entries AS entry
                         ON entry.entry_owner_kind = 'EXECUTED_TURN' AND entry.session_id = prior.session_id
                        AND entry.id = prior.subject_entry_id
                       WHERE prior.session_id = event.session_id
                         AND prior.event_sequence < event.event_sequence
                         AND entry.conversation_scope_kind = 'ROOT'
                   ), 0) AS adopted_after_entry_sequence
            FROM pulsara_v3.agent_events AS event
            JOIN pulsara_v3.turn_context_binding_revisions AS revision
              ON revision.session_id = event.session_id
             AND revision.id = event.subject_context_binding_revision_id
            JOIN pulsara_v3.turns AS turn
              ON turn.session_id = revision.session_id
             AND turn.id = revision.turn_id
            WHERE event.session_id = %s
              AND event.event_type = 'CompactionAdopted'
              AND turn.conversation_scope_kind = 'ROOT'
            ORDER BY event.event_sequence DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        result = wire.CanonicalControl(
            session_lifecycle=lifecycle,
            prompt_queue_total_count=queue_total,
        )
        genesis = connection.execute(
            "SELECT base_kind, source_through_sequence FROM pulsara_v3.session_context_genesis WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        if genesis is not None:
            result.initial_context_base.CopyFrom(wire.InitialContextBase(
                base_kind=str(genesis["base_kind"]),
                source_through_sequence=int(genesis["source_through_sequence"]),
                display_after_entry_sequence=int(genesis["source_through_sequence"]),
            ))
        latest_root = connection.execute(
            """SELECT id, status, terminal_reason FROM pulsara_v3.turns
               WHERE session_id = %s AND conversation_scope_kind = 'ROOT'
               ORDER BY accepted_at DESC, id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if latest_root is not None:
            result.latest_root_turn.CopyFrom(wire.LatestRootTurnControl(
                turn_id=str(latest_root["id"]),
                status=str(latest_root["status"]),
                terminal_reason=str(latest_root["terminal_reason"] or ""),
            ))
        for row in turns:
            target = result.active_turns.add(
                turn_id=str(row["id"]),
                scope_kind=_scope_kind(str(row["conversation_scope_kind"])),
                scope_subagent_task_id=str(row["scope_subagent_task_id"] or ""),
                status=str(row["status"]),
                accepted_at_utc=_utc(row["accepted_at"]),
            )
            if row["permission_snapshot_id"] is not None:
                target.permission.CopyFrom(_permission_projection(row))
        for row in queue:
            target = result.prompt_queue.add(
                queue_item_id=str(row["id"]),
                queue_sequence=int(row["queue_sequence"]),
                status=str(row["status"]),
                delivery_mode=str(row["delivery_mode"]),
                target_turn_id=str(row["target_turn_id"] or ""),
                content=_content_reference(row),
                command_id=str(row["command_id"]),
                accepted_at_utc=_utc(row["accepted_at"]),
            )
            if row["permission_snapshot_id"] is not None:
                target.permission.CopyFrom(_permission_projection(row))
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
                completion_accepted=row["accepted_root_entry_id"] is not None,
                batch_id=str(row["batch_id"] or ""),
                task_key=str(row["task_key"] or ""),
                label=str(row["label"] or ""),
                profile=str(row["profile_kind"]),
                display_role=str(row["display_role"] or ""),
                context_mode=str(row["context_mode"]),
                context_last_n_turns=int(row["context_last_n_turns"] or 0),
                pending_reason=str(row["pending_reason"] or ""),
                terminal_reason=str(row["terminal_reason"] or ""),
                terminal_public_detail=str(row["terminal_public_detail"] or ""),
                result_source=str(row["result_source"] or ""),
                result_summary=str(row["result_summary"] or ""),
                dependency_task_ids=tuple(row["dependency_task_ids"]),
            )
        if active_plan is not None:
            result.active_plan_workflow.CopyFrom(
                wire.PlanWorkflowControl(
                    workflow_id=str(active_plan["id"]),
                    workflow_ordinal=int(active_plan["workflow_ordinal"]),
                    workflow_revision=int(active_plan["workflow_revision"]),
                    status=str(active_plan["status"]),
                    entered_by=str(active_plan["entered_by"]),
                    resume_permission_mode=_permission_mode(
                        str(active_plan["resume_permission_mode"])
                    ),
                )
            )
        if open_plan is not None:
            frozen = freeze_json(dict(open_plan["tool_arguments"]))
            if not isinstance(frozen, FrozenJsonObjectFact):
                raise CanonicalProtocolResourceExhausted(
                    "open Plan content is not a canonical object"
                )
            binding = PlanInteractionBinding(
                str(open_plan["request_contract_id"]),
                str(open_plan["request_contract_version"]),
                str(open_plan["request_contract_fingerprint"]),
            )
            target = wire.PlanInteractionControl(
                interaction_id=str(open_plan["id"]),
                workflow_id=str(open_plan["plan_workflow_id"]),
                kind=str(open_plan["kind"]),
                status=str(open_plan["status"]),
                interaction_ordinal=int(open_plan["interaction_ordinal"]),
            )
            if str(open_plan["kind"]) == "QUESTION":
                question = extract_plan_question(
                    interaction_id=str(open_plan["id"]),
                    binding=binding,
                    arguments=frozen,
                )
                target.typed_content_fingerprint = question.typed_content_fingerprint
                target.option_count = len(question.options)
                target.allow_free_text = question.allow_free_text
            else:
                draft = extract_plan_draft(
                    interaction_id=str(open_plan["id"]),
                    assistant_entry_id=str(open_plan["assistant_entry_id"]),
                    tool_call_id=str(open_plan["tool_call_id"]),
                    binding=binding,
                    request_semantic_digest=str(open_plan["request_semantic_digest"]),
                    arguments=frozen,
                )
                target.draft_utf8_size = draft.identity.plan_utf8_size
                target.draft_utf8_digest = draft.identity.plan_utf8_digest
                target.summary_present = draft.summary is not None
            result.open_plan_interaction.CopyFrom(target)
        if latest_handoff is not None:
            claimed = (
                latest_handoff["claim_entry_id"] is not None
                or latest_handoff["claim_queue_item_id"] is not None
            )
            disposition = (
                wire.PLAN_HANDOFF_CLAIMED
                if claimed
                else (
                    wire.PLAN_HANDOFF_SUPERSEDED
                    if bool(latest_handoff["superseded"])
                    else wire.PLAN_HANDOFF_PENDING
                )
            )
            result.latest_plan_handoff.CopyFrom(
                wire.PlanHandoffControl(
                    workflow_id=str(latest_handoff["id"]),
                    interaction_id=str(latest_handoff["interaction_id"] or ""),
                    handoff_kind=(
                        "CANCELLED_PLAN"
                        if str(latest_handoff["status"]) == "CANCELLED"
                        else "FORCE_EXITED_PLAN"
                    ),
                    disposition=disposition,
                    claim_entry_id=str(latest_handoff["claim_entry_id"] or ""),
                    claim_queue_item_id=str(
                        latest_handoff["claim_queue_item_id"] or ""
                    ),
                    resume_permission_mode=_permission_mode(
                        str(latest_handoff["resume_permission_mode"])
                    ),
                )
            )
        if latest_context_compaction is not None:
            result.latest_context_compaction.CopyFrom(
                wire.ContextCompactionControl(
                    turn_id=str(latest_context_compaction["turn_id"]),
                    context_binding_revision_id=str(
                        latest_context_compaction["context_binding_revision_id"]
                    ),
                    source_through_sequence=int(
                        latest_context_compaction["source_through_sequence"]
                    ),
                    adopted_after_entry_sequence=int(
                        latest_context_compaction["adopted_after_entry_sequence"]
                    ),
                    accepted_at_utc=_utc(latest_context_compaction["accepted_at"]),
                )
            )
        return result


def _permission_mode(value: str) -> int:
    return {
        PermissionMode.ACCEPT_EDITS.value: wire.PERMISSION_MODE_ACCEPT_EDITS,
        PermissionMode.READ_ONLY.value: wire.PERMISSION_MODE_READ_ONLY,
        PermissionMode.ASK_PERMISSIONS.value: wire.PERMISSION_MODE_ASK_PERMISSIONS,
        PermissionMode.BYPASS_PERMISSIONS.value: (
            wire.PERMISSION_MODE_BYPASS_PERMISSIONS
        ),
    }[value]


def _permission_projection(row: Mapping[str, object]):
    return wire.RunPermissionProjection(
        permission_snapshot_id=str(row["permission_snapshot_id"]),
        requested_mode=_permission_mode(str(row["requested_permission_mode"])),
        effective_mode=_permission_mode(str(row["effective_permission_mode"])),
        admission_source=str(row["permission_admission_source"]),
        overlay=str(row["permission_overlay"]),
        plan_context_ordinal=int(row["permission_plan_context_ordinal"]),
        plan_workflow_id=str(row["permission_plan_workflow_id"] or ""),
        plan_workflow_revision=int(row["permission_plan_revision_at_admission"] or 0),
        inherited_from_turn_id=str(row["permission_inherited_from_turn_id"] or ""),
        contract_id=str(row["permission_contract_id"]),
        contract_fingerprint=str(row["permission_contract_fingerprint"]),
        snapshot_fingerprint=str(row["permission_snapshot_fingerprint"]),
    )


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


def _reasoning_content_reference(content: bytes) -> wire.CanonicalContentReference:
    inline = len(content) <= MAXIMUM_REASONING_INLINE_BYTES
    return wire.CanonicalContentReference(
        kind=wire.INLINE if inline else wire.CANONICAL_BLOB,
        inline_content=content if inline else b"",
        digest="sha256:" + sha256(content).hexdigest(),
        size=len(content),
        media_type="text/plain",
        codec="utf-8",
    )


def _event_subject(event: Mapping[str, object]) -> tuple[str, str]:
    present = tuple(
        (key, str(event[key]))
        for key in (
            "subject_turn_id",
            "subject_entry_id",
            "subject_tool_attempt_id",
            "subject_queue_item_id",
            "subject_interaction_decision_id",
            "subject_context_binding_revision_id",
            "subject_subagent_task_id",
            "subject_subagent_message_id",
            "subject_subagent_result_id",
            "subject_plan_workflow_id",
            "subject_plan_interaction_id",
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
