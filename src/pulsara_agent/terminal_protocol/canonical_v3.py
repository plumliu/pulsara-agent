"""Protocol-v3 read model over the canonical relational kernel.

Every public read is produced from one read-only repeatable-read transaction.
Occurrence rows select a closed projection branch but never prove canonical
subjects; the subject relation is loaded and validated independently.
"""

from __future__ import annotations

from pulsara_agent.conversation_kernel.interruption import (
    read_executed_notices,
    read_imported_notices,
    TurnInterruptionNotice,
)
from dataclasses import asdict

import codecs
import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from google.protobuf.message import Message
from google.protobuf.json_format import MessageToDict
from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.root_status import LATEST_ROOT_TURN_SQL
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


def browser_history_uint64(value: object, label: str) -> int:
    """Accept exact JSON/protobuf integers, never rounded floats or booleans."""
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        result = int(value)
    else:
        raise ValueError(f"{label} must be an exact uint64")
    if not 0 <= result <= (1 << 64) - 1:
        raise ValueError(f"{label} must be an exact uint64")
    return result


def _browser_wire(message: Message) -> dict[str, object]:
    return MessageToDict(message, preserving_proto_field_name=True)


def _browser_json_size(value: object) -> int:
    # Match aiohttp's default JSON encoder, including escaping and separators.
    return len(json.dumps(value).encode("utf-8"))


def _browser_coverage(rows) -> dict[str, str]:
    return {
        "first_entry_sequence": str(rows[0]["entry_sequence"]) if rows else "0",
        "last_entry_sequence": str(rows[-1]["entry_sequence"]) if rows else "0",
    }


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
            event_cut = int(session["latest_event_sequence"])
            selected_desc: list[wire.CanonicalEntry] = []
            notices: list[wire.TurnInterruptionNotice] = []
            snapshot = self._snapshot_value(
                session=session,
                session_id=session_id,
                cut=cut,
                entries=(),
                control=self._control(
                    connection,
                    session_id=session_id,
                    lifecycle=str(session["lifecycle"]),
                    maximum_items=maximum_control_items,
                    entry_sequence_floor=cut + 1,
                ),
                has_older=bool(rows),
                notices=(),
            )
            if _wire_size(snapshot) > maximum_serialized_bytes:
                raise CanonicalProtocolResourceExhausted(
                    "canonical control cannot fit snapshot"
                )
            for row in rows[:maximum_entries]:
                entry = self._entry(connection, row)
                mounted = self._notices(
                    connection, session_id, event_cut, (entry.entry_sequence,)
                )
                entries = tuple(reversed((*selected_desc, entry)))
                control = self._control(
                    connection,
                    session_id=session_id,
                    lifecycle=str(session["lifecycle"]),
                    maximum_items=maximum_control_items,
                    entry_sequence_floor=entry.entry_sequence,
                )
                candidate = self._snapshot_value(
                    session=session,
                    session_id=session_id,
                    cut=cut,
                    entries=entries,
                    control=control,
                    has_older=len(selected_desc) + 1 < len(rows),
                    notices=tuple((*notices, *mounted)),
                )
                if _wire_size(candidate) > maximum_serialized_bytes:
                    if not selected_desc:
                        raise CanonicalProtocolResourceExhausted(
                            "one entry and its notices cannot fit snapshot"
                        )
                    break
                selected_desc.append(entry)
                notices.extend(mounted)
                snapshot = candidate
            return snapshot

    @staticmethod
    def _notice_wire(notice: TurnInterruptionNotice) -> wire.TurnInterruptionNotice:
        values = asdict(notice)
        if values["public_detail"] is None:
            del values["public_detail"]
        return wire.TurnInterruptionNotice(**values)

    def _notices(
        self, connection, session_id: str, event_cut: int, sequences: tuple[int, ...]
    ) -> tuple[wire.TurnInterruptionNotice, ...]:
        facts = (
            *read_executed_notices(
                connection,
                session_id=session_id,
                event_cut=event_cut,
                entry_sequences=sequences,
            ),
            *read_imported_notices(
                connection, session_id=session_id, entry_sequences=sequences
            ),
        )
        return tuple(
            self._notice_wire(n)
            for n in sorted(
                facts,
                key=lambda n: (
                    n.display_after_entry_sequence,
                    n.owner_kind,
                    n.owner_id,
                ),
            )
        )

    def authorize_session(self, *, session_id: str, memory_domain_id: str,
                          deadline_monotonic: float) -> Mapping[str, object]:
        """Cold authorization shared by browser and model history reads."""
        with self._connection(deadline_monotonic) as connection:
            return self.authorized_session(connection, session_id, memory_domain_id)

    @staticmethod
    def authorized_session(connection, session_id: str, memory_domain_id: str):
        row = connection.execute(
            """SELECT s.* FROM pulsara_v3.sessions s
               JOIN pulsara_v3.workspaces w ON w.id=s.workspace_id
                 AND w.memory_domain_id=s.memory_domain_id
               WHERE s.id=%s AND s.memory_domain_id=%s""",
            (session_id, memory_domain_id),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row

    @staticmethod
    def validate_history_cut(connection, session, cut_sequence: int, event_sequence_cut: int):
        if (type(cut_sequence) is not int or type(event_sequence_cut) is not int
            or cut_sequence < 0 or event_sequence_cut < 0):
            raise ValueError("history cut is invalid")
        if (int(session["latest_entry_sequence"]) < cut_sequence
            or int(session["latest_event_sequence"]) < event_sequence_cut):
            raise CanonicalProtocolGap("history cut is ahead of canonical head")
        mixed_cut = connection.execute(
            """SELECT 1 FROM pulsara_v3.transcript_entries e
               JOIN pulsara_v3.agent_events ev ON ev.session_id=e.session_id
                 AND ev.subject_entry_id=e.id
               WHERE e.session_id=%s AND e.entry_sequence<=%s
                 AND ev.event_sequence>%s LIMIT 1""",
            (session["id"], cut_sequence, event_sequence_cut),
        ).fetchone()
        if mixed_cut is not None:
            raise CanonicalProtocolGap("history entry and event cuts disagree")

    def history_page(
        self,
        *,
        session_id: str,
        cut_sequence: int,
        event_sequence_cut: int,
        before_entry_sequence: int,
        maximum_entries: int,
        deadline_monotonic: float,
        maximum_serialized_bytes: int = MAXIMUM_HISTORY_PAGE_BYTES,
        request_id: str = "",
    ) -> wire.HistoryPageResponse:
        _bounded(maximum_entries, MAXIMUM_SNAPSHOT_ENTRIES, "history entries")
        _bounded_bytes(
            maximum_serialized_bytes, MAXIMUM_HISTORY_PAGE_BYTES, "history page bytes"
        )
        if (
            cut_sequence < 0
            or event_sequence_cut < 0
            or before_entry_sequence < 1
            or before_entry_sequence > cut_sequence + 1
        ):
            raise ValueError("history cursor is invalid")
        with self._connection(deadline_monotonic) as connection:
            session = self._session(connection, session_id)
            self.validate_history_cut(connection, session, cut_sequence, event_sequence_cut)
            rows = connection.execute(
                """SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s
                   AND entry_sequence<=%s AND entry_sequence<%s
                   ORDER BY entry_sequence DESC LIMIT %s""",
                (session_id, cut_sequence, before_entry_sequence, maximum_entries + 1),
            ).fetchall()
            selected_desc = []
            notices = []
            response = wire.HistoryPageResponse(request_id=request_id)
            if (
                _wire_size(wire.ServerFrame(history_page=response))
                > maximum_serialized_bytes
            ):
                raise CanonicalProtocolResourceExhausted(
                    "history envelope cannot fit its byte bound"
                )
            for row in rows[:maximum_entries]:
                entry = self._entry(connection, row)
                mounted = self._notices(
                    connection, session_id, event_sequence_cut, (entry.entry_sequence,)
                )
                candidate = wire.HistoryPageResponse(
                    request_id=request_id,
                    entries=tuple(reversed((*selected_desc, entry))),
                    interruption_notices=tuple((*notices, *mounted)),
                    has_more=len(selected_desc) + 1 < len(rows),
                )
                if candidate.has_more:
                    candidate.older_history_cursor.CopyFrom(
                        wire.HistoryCursor(
                            session_id=session_id,
                            cut_sequence=cut_sequence,
                            event_sequence_cut=event_sequence_cut,
                            entry_sequence=entry.entry_sequence,
                        )
                    )
                # Quote the full envelope, including cursor and all mounted notices.
                if (
                    _wire_size(wire.ServerFrame(history_page=candidate))
                    > maximum_serialized_bytes
                ):
                    if not selected_desc:
                        raise CanonicalProtocolResourceExhausted(
                            "one entry and its notices cannot fit history page"
                        )
                    break
                selected_desc.append(entry)
                notices.extend(mounted)
                response = candidate
            return response

    def browser_history_window(
        self,
        *,
        session_id: str,
        memory_domain_id: str,
        deadline_monotonic: float,
        anchor_entry_id: str | None = None,
        cursor: Mapping[str, object] | None = None,
        maximum_entries: int = STAGE2_LIMITS.history_page_default_entries,
        maximum_serialized_bytes: int = MAXIMUM_HISTORY_PAGE_BYTES,
    ) -> dict[str, object]:
        """A disposable browser window, independent of all execution cursors.

        SQL owns keyset selection; the existing canonical converter owns entry
        identity/content. The only adaptation is a JSON envelope with two edges.
        The anchor is admitted first, so neighbours cannot displace the target.
        """
        _bounded(maximum_entries, MAXIMUM_SNAPSHOT_ENTRIES, "history entries")
        _bounded_bytes(maximum_serialized_bytes, MAXIMUM_HISTORY_PAGE_BYTES, "history page bytes")
        if (anchor_entry_id is None) == (cursor is None):
            raise ValueError("exactly one history anchor or cursor is required")
        if anchor_entry_id is not None and (not isinstance(anchor_entry_id, str) or not anchor_entry_id):
            raise ValueError("history anchor must be an entry ID")
        with self._connection(deadline_monotonic) as connection:
            session = self.authorized_session(connection, session_id, memory_domain_id)
            if cursor is None:
                cut = int(session["latest_entry_sequence"])
                event_cut = int(session["latest_event_sequence"])
                direction = "anchor"
                anchor = connection.execute(
                    "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
                    (session_id, anchor_entry_id),
                ).fetchone()
                if anchor is None:
                    raise KeyError(anchor_entry_id)
                edge = int(anchor["entry_sequence"])
                before = connection.execute(
                    """SELECT * FROM pulsara_v3.transcript_entries
                       WHERE session_id=%s AND entry_sequence<%s
                       ORDER BY entry_sequence DESC LIMIT %s""",
                    (session_id, edge, maximum_entries - 1),
                ).fetchall()
                after = connection.execute(
                    """SELECT * FROM pulsara_v3.transcript_entries
                       WHERE session_id=%s AND entry_sequence>%s AND entry_sequence<=%s
                       ORDER BY entry_sequence LIMIT %s""",
                    (session_id, edge, cut, maximum_entries - 1),
                ).fetchall()
                candidates = [anchor]
                for index in range(max(len(before), len(after))):
                    if index < len(before):
                        candidates.append(before[index])
                    if index < len(after):
                        candidates.append(after[index])
                candidates = candidates[:maximum_entries]
            else:
                if cursor.get("session_id") != session_id:
                    raise ValueError("history cursor session conflicts")
                direction = cursor.get("direction")
                if direction not in {"older", "newer"}:
                    raise ValueError("history cursor direction is invalid")
                cut = browser_history_uint64(cursor.get("cut_sequence"), "history cut")
                event_cut = browser_history_uint64(cursor.get("event_sequence_cut"), "history event cut")
                edge = browser_history_uint64(cursor.get("entry_sequence"), "history edge")
                self._validate_browser_history_cut(connection, session, cut, event_cut)
                if not 1 <= edge <= cut:
                    raise ValueError("history edge is outside its cut")
                edge_row = connection.execute(
                    "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_sequence=%s",
                    (session_id, edge),
                ).fetchone()
                if edge_row is None:
                    raise KeyError("history cursor edge")
                # These SQL fragments are a closed server-side enum, not input.
                comparison, order = ("<", "DESC") if direction == "older" else (">", "ASC")
                candidates = connection.execute(
                    f"""SELECT * FROM pulsara_v3.transcript_entries
                        WHERE session_id=%s AND entry_sequence {comparison} %s AND entry_sequence<=%s
                        ORDER BY entry_sequence {order} LIMIT %s""",
                    (session_id, edge, cut, maximum_entries),
                ).fetchall()
            bounds = connection.execute(
                """SELECT min(entry_sequence) AS first, max(entry_sequence) AS last
                   FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_sequence<=%s""",
                (session_id, cut),
            ).fetchone()
            selected: list[Mapping[str, object]] = []
            entries: dict[str, dict[str, object]] = {}

            def envelope() -> dict[str, object]:
                ordered = sorted(selected, key=lambda row: int(row["entry_sequence"]))
                value: dict[str, object] = {
                    "session_id": session_id, "cut_sequence": str(cut),
                    "event_sequence_cut": str(event_cut), "direction": direction,
                    "entries": [entries[str(row["id"])] for row in ordered],
                    "coverage": _browser_coverage(ordered),
                }
                if anchor_entry_id is not None:
                    value["anchor_entry_id"] = anchor_entry_id
                for name, position, more in (
                    ("older", ordered[0] if ordered else None, bool(ordered and int(ordered[0]["entry_sequence"]) > bounds["first"])),
                    ("newer", ordered[-1] if ordered else None, bool(ordered and int(ordered[-1]["entry_sequence"]) < bounds["last"])),
                ):
                    if more:
                        value[name + "_cursor"] = {
                            "session_id": session_id, "cut_sequence": str(cut),
                            "event_sequence_cut": str(event_cut), "direction": name,
                            "entry_sequence": str(position["entry_sequence"]),
                        }
                return value

            response = envelope()
            if _browser_json_size(response) > maximum_serialized_bytes:
                raise CanonicalProtocolResourceExhausted("history window envelope cannot fit byte bound")
            for row in candidates:
                entries[str(row["id"])] = _browser_wire(self._entry(connection, row))
                selected.append(row)
                candidate = envelope()
                if _browser_json_size(candidate) > maximum_serialized_bytes:
                    selected.pop()
                    if not selected:
                        raise CanonicalProtocolResourceExhausted("history anchor or next entry cannot fit byte bound")
                    break
                response = candidate
            return response

    def browser_history_context(
        self,
        *,
        session_id: str,
        memory_domain_id: str,
        entry_ids: list[str],
        deadline_monotonic: float,
        boundary_event_ids: list[str] | None = None,
        cut_sequence: int | None = None,
        event_sequence_cut: int | None = None,
        maximum_serialized_bytes: int = MAXIMUM_HISTORY_PAGE_BYTES,
    ) -> dict[str, object]:
        """Enrich one bounded exact entry set, without reading transcript bodies.

        Initial/older/window pages and committed observation batches all call
        this same builder. Omitting both cuts asks for a fresh legal pair, not
        a synthetic maximum taken from a partial browser cache.
        """
        if not isinstance(entry_ids, list) or any(not isinstance(item, str) or not item for item in entry_ids):
            raise ValueError("history context entry IDs must be strings")
        if boundary_event_ids is None:
            boundary_event_ids = []
        if not isinstance(boundary_event_ids, list) or any(not isinstance(item, str) or not item for item in boundary_event_ids):
            raise ValueError("history context boundary event IDs must be strings")
        if len(entry_ids) + len(boundary_event_ids) > MAXIMUM_SNAPSHOT_ENTRIES:
            raise ValueError("history context exceeds the Protocol v3 entry bound")
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("history context entry IDs must be unique")
        if len(set(boundary_event_ids)) != len(boundary_event_ids):
            raise ValueError("history context boundary event IDs must be unique")
        if (cut_sequence is None) != (event_sequence_cut is None):
            raise ValueError("history context requires both cuts or neither")
        _bounded_bytes(maximum_serialized_bytes, MAXIMUM_HISTORY_PAGE_BYTES, "history context bytes")
        with self._connection(deadline_monotonic) as connection:
            session = self.authorized_session(connection, session_id, memory_domain_id)
            if cut_sequence is None:
                cut_sequence = int(session["latest_entry_sequence"])
                event_sequence_cut = int(session["latest_event_sequence"])
            else:
                self._validate_browser_history_cut(connection, session, cut_sequence, event_sequence_cut)
            rows = connection.execute(
                """SELECT id, entry_sequence, entry_owner_kind, turn_id, imported_history_group_id,
                          conversation_scope_kind, scope_subagent_task_id, source_subagent_task_id
                   FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=ANY(%s)
                   ORDER BY entry_sequence""",
                (session_id, entry_ids),
            ).fetchall()
            if len(rows) != len(entry_ids):
                raise KeyError("history context entry does not belong to session")
            if any(int(row["entry_sequence"]) > cut_sequence for row in rows):
                raise CanonicalProtocolGap("history context target is beyond its cut")
            response: dict[str, object] = {
                "session_id": session_id, "cut_sequence": str(cut_sequence),
                "event_sequence_cut": str(event_sequence_cut),
                "entry_ids": [str(row["id"]) for row in rows],
                "coverage": _browser_coverage(rows), "groups": [], "entry_contexts": [],
                "tasks": [], "batches": [], "interruption_notices": [], "compactions": [],
                "boundary_entries": [],
            }
            encoded_bytes = _browser_json_size(response)
            measured_lengths = {name: 0 for name in (
                "groups", "entry_contexts", "tasks", "batches", "interruption_notices", "compactions",
                "boundary_entries",
            )}

            def check_budget() -> None:
                nonlocal encoded_bytes
                # Sections only append complete values. Charge each item once,
                # including JSON's comma-space, rather than re-encoding a long
                # occurrence list quadratically before reporting its bound.
                for name, measured in measured_lengths.items():
                    values = response[name]
                    for index in range(measured, len(values)):
                        encoded_bytes += _browser_json_size(values[index]) + (2 if index else 0)
                    measured_lengths[name] = len(values)
                if encoded_bytes > maximum_serialized_bytes:
                    raise CanonicalProtocolResourceExhausted("necessary history context cannot fit byte bound")

            check_budget()
            self._browser_context_boundary_entries(
                connection, session_id, event_sequence_cut, boundary_event_ids, response, check_budget,
            )
            if not rows:
                return response
            self._browser_context_groups(connection, session_id, cut_sequence, event_sequence_cut, rows, response, check_budget)
            self._browser_context_tasks(connection, session_id, event_sequence_cut, rows, response, check_budget)
            sequences = tuple(int(row["entry_sequence"]) for row in rows)
            response["interruption_notices"] = [
                _browser_wire(self._notice_wire(notice)) for notice in (
                    *read_executed_notices(connection, session_id=session_id, event_cut=event_sequence_cut,
                                           entry_sequences=sequences, root_only=False),
                    *read_imported_notices(connection, session_id=session_id, entry_sequences=sequences),
                )
            ]
            check_budget()
            # Find each occurrence's actual ROOT mount. In particular an adoption
            # after the original page cut can still mount in that same old page.
            # Repeated adoptions may share one edge. Stream them so the byte
            # outcome stops the read without materializing an unbounded result.
            with connection.cursor(name="browser_history_compactions") as events:
                events.itersize = MAXIMUM_SNAPSHOT_ENTRIES
                events.execute(
                    """SELECT ev.event_id, ev.event_sequence, ev.accepted_at,
                              r.turn_id, r.id AS context_binding_revision_id, r.source_through_sequence,
                              coalesce(mount.entry_sequence, 0) AS adopted_after_entry_sequence
                       FROM pulsara_v3.agent_events ev
                       JOIN pulsara_v3.turn_context_binding_revisions r
                         ON r.session_id=ev.session_id AND r.id=ev.subject_context_binding_revision_id
                       JOIN pulsara_v3.turns t ON t.session_id=r.session_id AND t.id=r.turn_id
                       LEFT JOIN LATERAL (
                         SELECT e.entry_sequence FROM pulsara_v3.agent_events prior
                         JOIN pulsara_v3.transcript_entries e ON e.session_id=prior.session_id AND e.id=prior.subject_entry_id
                         WHERE prior.session_id=ev.session_id AND prior.event_sequence<ev.event_sequence
                           AND e.entry_owner_kind='EXECUTED_TURN' AND e.conversation_scope_kind='ROOT'
                         ORDER BY e.entry_sequence DESC LIMIT 1
                       ) mount ON true
                       WHERE ev.session_id=%s AND ev.event_type='CompactionAdopted'
                         AND ev.event_sequence<=%s AND t.conversation_scope_kind='ROOT'
                         AND (mount.entry_sequence=ANY(%s::bigint[]) OR
                              (mount.entry_sequence IS NULL AND EXISTS (
                                SELECT 1 FROM pulsara_v3.transcript_entries first_entry
                                WHERE first_entry.session_id=ev.session_id AND first_entry.id=ANY(%s)
                                  AND NOT EXISTS (SELECT 1 FROM pulsara_v3.transcript_entries earlier
                                    WHERE earlier.session_id=first_entry.session_id
                                      AND earlier.entry_sequence<first_entry.entry_sequence))))
                       ORDER BY ev.event_sequence""",
                    (session_id, event_sequence_cut, list(sequences), entry_ids),
                )
                for event in events:
                    response["compactions"].append({
                        "event_id": str(event["event_id"]), "event_sequence": str(event["event_sequence"]),
                        "turn_id": str(event["turn_id"]), "context_binding_revision_id": str(event["context_binding_revision_id"]),
                        "source_through_sequence": str(event["source_through_sequence"]),
                        "adopted_after_entry_sequence": str(event["adopted_after_entry_sequence"]),
                        "accepted_at_utc": _utc(event["accepted_at"]),
                    })
                    check_budget()
            return response

    @staticmethod
    def _browser_context_boundary_entries(connection, session_id, event_cut, event_ids, response, check_budget):
        if not event_ids:
            return
        # Exact committed occurrence IDs locate the page to invalidate. Current
        # coalesced control (in particular its latest adoption) is not evidence
        # that an earlier occurrence has been observed or its page refreshed.
        events = connection.execute(
            """SELECT ev.event_id, ev.event_sequence, ev.event_type,
                      t.id AS owner_turn_id, t.conversation_scope_kind,
                      mount.id AS entry_id, mount.entry_sequence
               FROM pulsara_v3.agent_events ev
               LEFT JOIN pulsara_v3.turn_context_binding_revisions r
                 ON r.session_id=ev.session_id AND r.id=ev.subject_context_binding_revision_id
               LEFT JOIN pulsara_v3.turns t ON t.session_id=ev.session_id
                 AND t.id=coalesce(ev.subject_turn_id, r.turn_id)
               LEFT JOIN LATERAL (
                 SELECT e.id, e.entry_sequence FROM pulsara_v3.transcript_entries e
                 JOIN pulsara_v3.agent_events accepted
                   ON accepted.session_id=e.session_id AND accepted.subject_entry_id=e.id
                 WHERE e.session_id=ev.session_id AND e.entry_owner_kind='EXECUTED_TURN'
                   AND accepted.event_sequence<ev.event_sequence
                   AND ((ev.event_type='TurnInterrupted' AND e.turn_id=t.id)
                     OR (ev.event_type='CompactionAdopted' AND (
                       (t.conversation_scope_kind='ROOT' AND e.conversation_scope_kind='ROOT')
                       OR (t.conversation_scope_kind='SUBAGENT_TASK' AND e.turn_id=t.id))))
                 ORDER BY e.entry_sequence DESC LIMIT 1
               ) mount ON true
               WHERE ev.session_id=%s AND ev.event_id=ANY(%s)
               ORDER BY ev.event_sequence""", (session_id, event_ids),
        ).fetchall()
        if len(events) != len(event_ids):
            raise KeyError("history boundary occurrence does not belong to session")
        for event in events:
            if int(event["event_sequence"]) > event_cut:
                raise CanonicalProtocolGap("history boundary occurrence is beyond its cut")
            if event["event_type"] not in {"CompactionAdopted", "TurnInterrupted"}:
                raise ValueError("history boundary target is not a boundary occurrence")
            if event["owner_turn_id"] is None or event["entry_id"] is None:
                raise ValueError("history boundary occurrence has no canonical owner mount")
            response["boundary_entries"].append({
                "event_id": str(event["event_id"]), "event_sequence": str(event["event_sequence"]),
                "entry_id": str(event["entry_id"]), "entry_sequence": str(event["entry_sequence"]),
            })
            check_budget()

    def _validate_browser_history_cut(self, connection, session, cut, event_cut):
        self.validate_history_cut(connection, session, cut, event_cut)
        # A legal pair describes the full entry head at that event cut. Imported
        # entries are present at genesis and have no entry-acceptance occurrence.
        # Do not accept a browser's arbitrary partial-cache maximum as a cut.
        head = connection.execute(
            """SELECT coalesce(max(e.entry_sequence), 0) AS head
               FROM pulsara_v3.transcript_entries e WHERE e.session_id=%s
                 AND (e.entry_owner_kind='IMPORTED_HISTORY' OR EXISTS (
                   SELECT 1 FROM pulsara_v3.agent_events ev
                   WHERE ev.session_id=e.session_id AND ev.subject_entry_id=e.id
                     AND ev.event_sequence<=%s))""", (session["id"], event_cut),
        ).fetchone()
        if int(head["head"]) != cut:
            raise CanonicalProtocolGap("history entry and event cuts are not a paired head")

    def _browser_context_groups(self, connection, session_id, cut, event_cut, rows, response, check_budget):
        groups: dict[tuple[str, str], dict[str, object]] = {}
        imported_interruptions: dict[str, int] = {}

        def group(kind: str, owner_id: str) -> dict[str, object]:
            key = (kind, owner_id)
            if key in groups:
                return groups[key]
            owner_column = "turn_id" if kind == "EXECUTED_TURN" else "imported_history_group_id"
            owner_table = "turns" if kind == "EXECUTED_TURN" else "imported_history_groups"
            interruption = "NULL" if kind == "EXECUTED_TURN" else "owner.interruption_outcome"
            boundary = connection.execute(
                f"""SELECT first_entry.id AS initial_entry_id, first_entry.entry_sequence AS first_sequence,
                           first_entry.conversation_scope_kind, first_entry.scope_subagent_task_id,
                           last_entry.entry_sequence AS last_sequence, owner.final_entry_id,
                           {interruption} AS interruption_outcome
                    FROM pulsara_v3.{owner_table} owner
                    JOIN LATERAL (SELECT * FROM pulsara_v3.transcript_entries e
                      WHERE e.session_id=owner.session_id AND e.{owner_column}=owner.id AND e.entry_sequence<=%s
                      ORDER BY e.entry_sequence LIMIT 1) first_entry ON true
                    JOIN LATERAL (SELECT e.entry_sequence FROM pulsara_v3.transcript_entries e
                      WHERE e.session_id=owner.session_id AND e.{owner_column}=owner.id AND e.entry_sequence<=%s
                      ORDER BY e.entry_sequence DESC LIMIT 1) last_entry ON true
                    WHERE owner.session_id=%s AND owner.id=%s""",
                (cut, cut, session_id, owner_id),
            ).fetchone()
            if boundary is None:
                raise KeyError("history context owner has no entry at cut")
            value: dict[str, object] = {
                "owner_kind": kind, "owner_id": owner_id,
                "scope_kind": str(boundary["conversation_scope_kind"]),
                "initial_entry_id": str(boundary["initial_entry_id"]),
                "first_entry_sequence": str(boundary["first_sequence"]),
                "last_entry_sequence": str(boundary["last_sequence"]),
            }
            if boundary["scope_subagent_task_id"]:
                value["scope_subagent_task_id"] = str(boundary["scope_subagent_task_id"])
            if boundary["final_entry_id"]:
                value["final_entry_id"] = str(boundary["final_entry_id"])
            if boundary["interruption_outcome"]:
                imported_interruptions[owner_id] = int(boundary["interruption_outcome"]["display_after_entry_sequence"])
            if value["scope_kind"] == "ROOT":
                # Ordering is by canonical group starts, not last activity or
                # adjacency in the browser's partial ROOT array.
                for direction, comparison, order in (("previous", "<", "DESC"), ("next", ">", "ASC")):
                    neighbour = connection.execute(
                        f"""WITH roots AS (
                          SELECT t.id AS owner_id, e.entry_sequence FROM pulsara_v3.turns t
                          JOIN pulsara_v3.transcript_entries e ON e.session_id=t.session_id AND e.id=t.initial_entry_id
                          WHERE t.session_id=%s AND t.conversation_scope_kind='ROOT'
                          UNION ALL
                          SELECT g.id AS owner_id, first_entry.entry_sequence FROM pulsara_v3.imported_history_groups g
                          JOIN LATERAL (SELECT e.entry_sequence FROM pulsara_v3.transcript_entries e
                            WHERE e.session_id=g.session_id AND e.imported_history_group_id=g.id
                            ORDER BY e.entry_sequence LIMIT 1) first_entry ON true
                          WHERE g.session_id=%s)
                        SELECT owner_id FROM roots WHERE entry_sequence {comparison} %s AND entry_sequence<=%s
                        ORDER BY entry_sequence {order} LIMIT 1""",
                        (session_id, session_id, boundary["first_sequence"], cut),
                    ).fetchone()
                    if neighbour:
                        value[direction + "_root_owner_id"] = str(neighbour["owner_id"])
            groups[key] = value
            response["groups"].append(value)
            check_budget()
            return value

        root_cache: dict[str, tuple[str, str | None]] = {}
        for row in rows:
            kind = str(row["entry_owner_kind"])
            owner_id = str(row["turn_id"] if kind == "EXECUTED_TURN" else row["imported_history_group_id"])
            owner = group(kind, owner_id)
            context: dict[str, object] = {
                "entry_id": str(row["id"]), "owner_kind": kind, "owner_id": owner_id,
                "segment_id": f"{kind}:{owner_id}:initial",
                "segment_start_entry_id": owner["initial_entry_id"],
            }
            if row["conversation_scope_kind"] == "ROOT":
                context["root_owner_id"] = owner_id
            elif kind == "EXECUTED_TURN":
                if owner_id not in root_cache:
                    ancestry = connection.execute(
                        """WITH RECURSIVE parents AS (
                          SELECT t.id, t.conversation_scope_kind, task.parent_turn_id
                          FROM pulsara_v3.turns t LEFT JOIN pulsara_v3.subagent_tasks task
                            ON task.session_id=t.session_id AND task.id=t.scope_subagent_task_id
                          WHERE t.session_id=%s AND t.id=%s
                          UNION ALL
                          SELECT t.id, t.conversation_scope_kind, task.parent_turn_id
                          FROM parents p JOIN pulsara_v3.turns t ON t.id=p.parent_turn_id AND t.session_id=%s
                          LEFT JOIN pulsara_v3.subagent_tasks task ON task.session_id=t.session_id AND task.id=t.scope_subagent_task_id)
                        SELECT (SELECT id FROM parents WHERE conversation_scope_kind='ROOT') AS root_id,
                               (SELECT parent_turn_id FROM parents WHERE id=%s) AS parent_id""",
                        (session_id, owner_id, session_id, owner_id),
                    ).fetchone()
                    if ancestry["root_id"] is None:
                        raise ValueError("canonical task owner has no ROOT ancestor")
                    root_cache[owner_id] = (str(ancestry["root_id"]), ancestry["parent_id"])
                root_id, parent_id = root_cache[owner_id]
                context["root_owner_id"] = root_id
                if parent_id:
                    context["parent_turn_id"] = str(parent_id)
                group("EXECUTED_TURN", root_id)
            owner_column = "turn_id" if kind == "EXECUTED_TURN" else "imported_history_group_id"
            neighbours = connection.execute(
                f"""SELECT
                    (SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND {owner_column}=%s
                     AND entry_sequence<%s ORDER BY entry_sequence DESC LIMIT 1) AS previous_id,
                    (SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND {owner_column}=%s
                     AND entry_sequence>%s AND entry_sequence<=%s ORDER BY entry_sequence LIMIT 1) AS next_id""",
                (session_id, owner_id, row["entry_sequence"], session_id, owner_id, row["entry_sequence"], cut),
            ).fetchone()
            if neighbours["previous_id"]:
                context["previous_entry_id"] = str(neighbours["previous_id"])
            if neighbours["next_id"]:
                context["next_entry_id"] = str(neighbours["next_id"])
            if kind == "EXECUTED_TURN":
                segment = connection.execute(
                    """SELECT ev.event_id, coalesce(mount.entry_sequence, 0) AS boundary
                       FROM pulsara_v3.agent_events ev
                       LEFT JOIN pulsara_v3.turn_context_binding_revisions r
                         ON r.session_id=ev.session_id AND r.id=ev.subject_context_binding_revision_id
                       LEFT JOIN LATERAL (
                         SELECT e.entry_sequence FROM pulsara_v3.agent_events prior
                         JOIN pulsara_v3.transcript_entries e ON e.session_id=prior.session_id AND e.id=prior.subject_entry_id
                         WHERE prior.session_id=ev.session_id AND prior.event_sequence<ev.event_sequence
                           AND e.turn_id=%s ORDER BY e.entry_sequence DESC LIMIT 1
                       ) mount ON true
                       WHERE ev.session_id=%s AND ev.event_sequence<=%s
                         AND ((ev.event_type='CompactionAdopted' AND r.turn_id=%s) OR
                              (ev.event_type='TurnInterrupted' AND ev.subject_turn_id=%s))
                         AND coalesce(mount.entry_sequence, 0)<%s
                       ORDER BY ev.event_sequence DESC LIMIT 1""",
                    (owner_id, session_id, event_cut, owner_id, owner_id, row["entry_sequence"]),
                ).fetchone()
                if segment:
                    first = connection.execute(
                        """SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s
                           AND entry_sequence>%s AND entry_sequence<=%s ORDER BY entry_sequence LIMIT 1""",
                        (session_id, owner_id, segment["boundary"], cut),
                    ).fetchone()
                    context["segment_id"] = f"{kind}:{owner_id}:{segment['event_id']}"
                    context["segment_start_entry_id"] = str(first["id"])
            elif owner_id in imported_interruptions and int(row["entry_sequence"]) > imported_interruptions[owner_id]:
                first = connection.execute(
                    """SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND imported_history_group_id=%s
                       AND entry_sequence>%s AND entry_sequence<=%s ORDER BY entry_sequence LIMIT 1""",
                    (session_id, owner_id, imported_interruptions[owner_id], cut),
                ).fetchone()
                context["segment_id"] = f"{kind}:{owner_id}:interruption"
                context["segment_start_entry_id"] = str(first["id"])
            response["entry_contexts"].append(context)
            check_budget()
        # Completion notifications can reference a task created in an earlier
        # ROOT. Supply that exact parent's relation, even when its entries are
        # outside this page; never infer "previous" from visible ROOTs.
        task_ids = {
            str(row[field]) for row in rows if row["entry_owner_kind"] == "EXECUTED_TURN"
            for field in ("scope_subagent_task_id", "source_subagent_task_id") if row[field]
        }
        for parent in connection.execute(
            """SELECT DISTINCT parent_turn_id FROM pulsara_v3.subagent_tasks
               WHERE session_id=%s AND id=ANY(%s)""", (session_id, list(task_ids)),
        ):
            group("EXECUTED_TURN", str(parent["parent_turn_id"]))

    @staticmethod
    def _browser_context_tasks(connection, session_id, event_cut, rows, response, check_budget):
        from pulsara_agent.identity import derive_id

        # Imported source labels are provenance only; they cannot authorize a
        # task lookup or synthesize a child owner in the forked session.
        task_ids = {
            str(row[field]) for row in rows if row["entry_owner_kind"] == "EXECUTED_TURN"
            for field in ("scope_subagent_task_id", "source_subagent_task_id") if row[field]
        }
        for task in connection.execute(
            """SELECT task.id, task.parent_turn_id, task.batch_id, parent.initial_entry_id
               FROM pulsara_v3.subagent_tasks task JOIN pulsara_v3.turns parent
                 ON parent.session_id=task.session_id AND parent.id=task.parent_turn_id
               WHERE task.session_id=%s AND task.id=ANY(%s) ORDER BY task.id""",
            (session_id, list(task_ids)),
        ):
            value = {
                "task_id": str(task["id"]), "parent_turn_id": str(task["parent_turn_id"]),
                "parent_initial_entry_id": str(task["initial_entry_id"]),
            }
            if task["batch_id"]:
                value["batch_id"] = str(task["batch_id"])
            response["tasks"].append(value)
            check_budget()
        entry_ids = [str(row["id"]) for row in rows if row["entry_owner_kind"] == "EXECUTED_TURN"]
        # Forward-only batch derivation from the unique physical attempt. No
        # transcript JSON decoding, inverse hashes, or scan of the entire ROOT.
        with connection.cursor(name="browser_history_creators") as attempts:
            attempts.itersize = MAXIMUM_SNAPSHOT_ENTRIES
            attempts.execute(
                """SELECT a.id, a.assistant_entry_id, a.tool_call_id, e.turn_id
                   FROM pulsara_v3.tool_execution_attempts a
                   JOIN pulsara_v3.transcript_entries e ON e.session_id=a.session_id AND e.id=a.assistant_entry_id
                   JOIN pulsara_v3.assistant_message_blocks b ON b.session_id=a.session_id
                     AND b.assistant_entry_id=a.assistant_entry_id AND b.tool_call_id=a.tool_call_id
                   WHERE a.session_id=%s AND e.entry_owner_kind='EXECUTED_TURN'
                     AND b.tool_name IN ('spawn_agent', 'create_agent_tasks')
                     AND (a.assistant_entry_id=ANY(%s) OR a.assistant_entry_id IN (
                       SELECT r.tool_call_entry_id FROM pulsara_v3.tool_results r
                       WHERE r.session_id=a.session_id AND r.result_entry_id=ANY(%s)))
                   ORDER BY a.assistant_entry_id, a.tool_call_id""",
                (session_id, entry_ids, entry_ids),
            )
            for attempt in attempts:
                batch_id = derive_id("batch", "spawn", str(attempt["id"]))
                count = connection.execute(
                    """SELECT count(*) AS count FROM pulsara_v3.subagent_tasks task
                       WHERE task.session_id=%s AND task.parent_turn_id=%s AND task.batch_id=%s
                         AND EXISTS (SELECT 1 FROM pulsara_v3.agent_events ev
                           WHERE ev.session_id=task.session_id AND ev.subject_subagent_task_id=task.id
                             AND ev.event_type='SubagentTaskAccepted' AND ev.event_sequence<=%s)""",
                    (session_id, attempt["turn_id"], batch_id, event_cut),
                ).fetchone()["count"]
                if count:
                    response["batches"].append({
                        "batch_id": batch_id, "parent_turn_id": str(attempt["turn_id"]),
                        "creator_entry_id": str(attempt["assistant_entry_id"]),
                        "creator_tool_call_id": str(attempt["tool_call_id"]), "task_count": str(count),
                    })
                    check_budget()

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
        notices: tuple[wire.TurnInterruptionNotice, ...],
    ) -> wire.CanonicalSessionSnapshot:
        snapshot = wire.CanonicalSessionSnapshot(
            session_id=session_id,
            workspace_id=str(session["workspace_id"]),
            writer_generation=int(session["writer_generation"]),
            entry_sequence_cut=cut,
            event_sequence_cut=int(session["latest_event_sequence"]),
            entries=entries,
            control=control,
            interruption_notices=notices,
        )
        if has_older:
            snapshot.older_history_cursor.CopyFrom(
                wire.HistoryCursor(
                    session_id=session_id,
                    cut_sequence=cut,
                    event_sequence_cut=int(session["latest_event_sequence"]),
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
            control: wire.CanonicalControl | None = None
            result: list[wire.CommittedObservationProjection] = []
            total = 0
            through = after_event_sequence
            for event in events[:maximum_events]:
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
                if subject_slot == "subject_subagent_task_id":
                    projection.affected_subagent_task_id = subject_id
                elif subject_slot == "subject_subagent_result_id":
                    child = connection.execute(
                        """SELECT task_id FROM pulsara_v3.subagent_task_children
                           WHERE session_id = %s AND id = %s AND child_kind = 'RESULT'""",
                        (session_id, subject_id),
                    ).fetchone()
                    if child is None:
                        raise RuntimeError("committed result subject is missing")
                    projection.affected_subagent_task_id = str(child["task_id"])
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
                    if (
                        event_type
                        == CommittedEventType.INTER_AGENT_MESSAGE_ACCEPTED.value
                    ):
                        task_id = (
                            row["source_subagent_task_id"]
                            if row["conversation_scope_kind"] == "ROOT"
                            else row["scope_subagent_task_id"]
                        )
                        if task_id is None:
                            raise RuntimeError(
                                "accepted inter-agent message lacks task identity"
                            )
                        projection.affected_subagent_task_id = str(task_id)
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
                        projection.control_read_event_sequence = high_water
                    else:
                        # The event remains ordered and visible; only its
                        # duplicate derived snapshot is coalesced in this batch.
                        projection.projection_kind = wire.CURRENT_CONTROL
                else:
                    projection.projection_kind = wire.EVENT_ONLY
                if event_type == CommittedEventType.TURN_INTERRUPTED.value:
                    facts = read_executed_notices(
                        connection,
                        session_id=session_id,
                        event_cut=int(event["event_sequence"]),
                        turn_id=subject_id,
                    )
                    if facts:
                        if len(facts) != 1:
                            raise RuntimeError("interruption occurrence is not unique")
                        projection.interruption_notice.CopyFrom(
                            self._notice_wire(facts[0])
                        )
                if projection.affected_subagent_task_id and not projection.HasField(
                    "current_control"
                ):
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
                    if not any(item.HasField("current_control") for item in result):
                        projection.current_control.CopyFrom(control)
                        projection.control_read_event_sequence = high_water
                if projection.affected_subagent_task_id:
                    task = connection.execute(
                        "SELECT batch_id FROM pulsara_v3.subagent_tasks WHERE session_id=%s AND id=%s",
                        (session_id, projection.affected_subagent_task_id),
                    ).fetchone()
                    if task is None:
                        raise RuntimeError("affected subagent task is absent")
                    projection.affected_subagent_batch_id = str(task["batch_id"])
                total += len(projection.SerializeToString(deterministic=True))
                if total > maximum_bytes:
                    if not result:
                        return CanonicalObservationBatch(
                            through_event_sequence=after_event_sequence,
                            projections=(),
                            gap_reason="COMMITTED_EVENT_TOO_LARGE",
                        )
                    break
                result.append(projection)
                through = int(event["event_sequence"])
            return CanonicalObservationBatch(through, tuple(result))

    def resolve_content_reference(
        self,
        *,
        session_id: str,
        deadline_monotonic: float,
        entry_id: str | None = None,
        queue_item_id: str | None = None,
        block_id: str | None = None,
        image_ref_ordinal: int | None = None,
        visualization_ordinal: int | None = None,
        image_ref: str | None = None,
    ) -> Mapping[str, object]:
        """Re-authorize an exact entry/block content edge before blob hydration."""
        if sum(value is not None for value in (entry_id, queue_item_id, image_ref)) != 1:
            raise ValueError("exactly one content target is required")
        if image_ref is not None and any(value is not None for value in (block_id, image_ref_ordinal, visualization_ordinal)):
            raise ValueError("image reference content target is invalid")
        if image_ref_ordinal is not None and (
            isinstance(image_ref_ordinal, bool) or image_ref_ordinal < 0
        ):
            raise ValueError("prompt image ordinal is invalid")
        if queue_item_id is not None and block_id is not None:
            raise ValueError("queue content has no block target")
        if image_ref_ordinal is not None and block_id is not None:
            raise ValueError("prompt image content has no block target")
        if visualization_ordinal is not None and (
            isinstance(visualization_ordinal, bool)
            or visualization_ordinal < 0
            or entry_id is None
            or block_id is not None
            or image_ref_ordinal is not None
        ):
            raise ValueError("visualization content target is invalid")
        with self._connection(deadline_monotonic) as connection:
            session = self._session(connection, session_id)
            if image_ref is not None:
                from pulsara_agent.conversation_kernel.prompt_storage import read_image_reference_in_connection
                from pulsara_agent.conversation_kernel.prompt_content import MAXIMUM_PROMPT_MULTIPART_BYTES
                image = read_image_reference_in_connection(
                    connection, session_id=session_id, workspace_id=str(session["workspace_id"]),
                    image_ref=image_ref, maximum_encoded_bytes=MAXIMUM_PROMPT_MULTIPART_BYTES,
                )
                return {"validated_image": image, "inline_content": None,
                        "blob_id": image.image_ref, "content_digest": "sha256:" + sha256(image.immutable_bytes).hexdigest(),
                        "content_size": len(image.immutable_bytes), "content_media_type": image.media_type,
                        "content_codec": "binary", "image_ref": image.image_ref, "width": image.width, "height": image.height}
            if visualization_ordinal is not None:
                from pulsara_agent.conversation_kernel.visualization import saved_visualizations
                items = saved_visualizations(connection, session_id, entry_id)
                if visualization_ordinal >= len(items) or items[visualization_ordinal]["state"] != "READY":
                    raise KeyError(entry_id)
                facts = items[visualization_ordinal]
                row = connection.execute(
                    """SELECT session_id, workspace_id, NULL::bytea AS inline_content, blob_id
                       FROM pulsara_v3.assistant_visualizations
                       WHERE session_id=%s AND assistant_entry_id=%s AND ordinal=%s""",
                    (session_id, entry_id, visualization_ordinal),
                ).fetchone()
                row.update(content_digest=facts["content_digest"], content_size=facts["content_size"],
                           content_media_type=facts["media_type"], content_codec=facts["codec"])
                if "image_ref" in facts:
                    row.update({key: facts[key] for key in ("image_ref", "width", "height")})
            elif queue_item_id is not None:
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
                    and str(row["entry_kind"])
                    not in {"USER_MESSAGE", "USER_STEER", "TOOL_RESULT"}
                ):
                    raise ValueError("entry kind cannot own image occurrences")
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
        read_connection=None,
    ) -> Mapping[str, object]:
        """Resolve one browser-visible result entry to its canonical artifact edge."""
        with (nullcontext(read_connection) if read_connection is not None else self._connection(deadline_monotonic)) as connection:
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
        root_assistant = (
            row["entry_kind"] == "ASSISTANT_MESSAGE"
            and row["conversation_scope_kind"] == "ROOT"
        )
        root_final = False
        if root_assistant:
            final = connection.execute(
                """SELECT CASE e.entry_owner_kind
                         WHEN 'EXECUTED_TURN' THEN
                           t.status IN ('COMPLETED', 'INTERRUPTED') AND t.final_entry_id = e.id
                         WHEN 'IMPORTED_HISTORY' THEN
                           g.status IN ('COMPLETED', 'INTERRUPTED') AND g.final_entry_id = e.id
                         ELSE FALSE END AS root_final
                   FROM pulsara_v3.transcript_entries AS e
                   LEFT JOIN pulsara_v3.turns AS t
                     ON e.entry_owner_kind = 'EXECUTED_TURN'
                    AND t.session_id = e.session_id AND t.id = e.turn_id
                   LEFT JOIN pulsara_v3.imported_history_groups AS g
                     ON e.entry_owner_kind = 'IMPORTED_HISTORY'
                    AND g.session_id = e.session_id AND g.id = e.imported_history_group_id
                   WHERE e.session_id = %s AND e.id = %s""",
                (row["session_id"], entry_id),
            ).fetchone()
            root_final = bool(final and final["root_final"])
        result = wire.CanonicalEntry(
            entry_id=entry_id,
            turn_id=str(row["turn_id"] if row["entry_owner_kind"] == "EXECUTED_TURN" else row["imported_history_group_id"]),
            entry_owner_kind=str(row["entry_owner_kind"]),
            root_final=root_final,
            fork_eligible=(
                root_assistant
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
        if row["scheduled_input"] is not None:
            result.scheduled_input.CopyFrom(wire.ScheduledInputProvenance(**row["scheduled_input"]))
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
                # A display prefix can end in the middle of a UTF-8 code point.
                # Let the stdlib decoder retain that unfinished suffix; the
                # canonical arguments and their digest remain complete.
                tool_arguments_preview=codecs.utf_8_decode(
                    arguments[:MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES], "strict", False
                )[0].encode("utf-8"),
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
        if row["entry_kind"] == "ASSISTANT_MESSAGE":
            from pulsara_agent.conversation_kernel.visualization import saved_visualizations
            for facts in saved_visualizations(connection, str(row["session_id"]), entry_id):
                result.visualizations.add(**facts)
        return result

    @staticmethod
    def _reasoning_blocks(
        connection: Any,
        entry: Mapping[str, object],
    ) -> tuple[ProviderVisibleReasoningBlock, ...]:
        entry_kind = str(entry["entry_kind"])
        if entry_kind not in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"}:
            return ()
        row = connection.execute(
            """
            SELECT assistant_entry_kind, codec_kind, payload_bytes,
                   payload_digest, payload_size, item_count
            FROM pulsara_v3.provider_assistant_replay_fragments
            WHERE session_id = %s AND assistant_entry_id = %s
            """,
            (entry["session_id"], entry["id"]),
        ).fetchone()
        if row is None:
            return ()
        if str(row["assistant_entry_kind"]) != entry_kind:
            raise RuntimeError("assistant provider replay kind drifted")
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

        active_turn_total = int(connection.execute(
            "SELECT count(*) AS total FROM pulsara_v3.turns WHERE session_id = %s AND status = 'RUNNING'",
            (session_id,),
        ).fetchone()["total"])
        turns = tuple(connection.execute(
            """SELECT * FROM pulsara_v3.turns WHERE session_id = %s AND status = 'RUNNING'
               ORDER BY CASE WHEN conversation_scope_kind = 'ROOT' THEN 0 ELSE 1 END,
                        accepted_at, id LIMIT %s""",
            (session_id, maximum_items),
        ).fetchall())
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
        tool_attempt_total = int(connection.execute(
            """SELECT count(*) AS total
               FROM pulsara_v3.tool_execution_attempts AS a
               JOIN pulsara_v3.transcript_entries AS e
                 ON e.entry_owner_kind = 'EXECUTED_TURN' AND e.session_id = a.session_id AND e.id = a.assistant_entry_id
               JOIN pulsara_v3.turns AS t ON t.session_id = e.session_id AND t.id = e.turn_id
               LEFT JOIN pulsara_v3.tool_results AS r ON r.session_id = a.session_id AND r.attempt_id = a.id
               WHERE a.session_id = %s AND r.id IS NULL
                 AND (e.entry_sequence >= %s OR t.status = 'RUNNING')""",
            (session_id, entry_sequence_floor),
        ).fetchone()["total"])
        attempts = tuple(connection.execute(
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
               ORDER BY CASE WHEN e.conversation_scope_kind = 'ROOT' THEN 0 ELSE 1 END,
                        a.started_at, a.id LIMIT %s""",
            (session_id, entry_sequence_floor, maximum_items),
        ).fetchall())
        task_counts = connection.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE t.status IN ('PENDING_START', 'WAITING_DEPENDENCY', 'ACTIVE')
                        OR (t.status IN ('COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED',
                                         'BLOCKED_DEPENDENCY_FAILED') AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.transcript_entries AS accepted
                          WHERE accepted.entry_owner_kind = 'EXECUTED_TURN'
                            AND accepted.session_id = t.session_id
                            AND accepted.source_subagent_task_id = t.id
                        ))) AS unreceived
               FROM pulsara_v3.subagent_tasks AS t WHERE t.session_id = %s""",
            (session_id,),
        ).fetchone()
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
            active_turn_total_count=active_turn_total,
            tool_attempt_total_count=tool_attempt_total,
            task_total_count=int(task_counts["total"]),
            task_unreceived_count=int(task_counts["unreceived"]),
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
            LATEST_ROOT_TURN_SQL,
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
                input_origin=str(row["input_origin"]),
                delivery_mode=str(row["delivery_mode"]),
                target_turn_id=str(row["target_turn_id"] or ""),
                content=_content_reference(row),
                command_id=str(row["command_id"]),
                accepted_at_utc=_utc(row["accepted_at"]),
            )
            if row["input_origin"] == "SCHEDULED_TASK":
                target.scheduled_input.CopyFrom(wire.ScheduledInputProvenance(
                    task_id=str(row["scheduled_task_id"]), task_revision=int(row["scheduled_task_revision"]),
                    due_at_utc=_utc(row["scheduled_due_at"]),
                    trigger_source=row["scheduled_trigger_source"]))
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
