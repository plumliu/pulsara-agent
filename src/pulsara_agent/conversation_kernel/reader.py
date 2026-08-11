"""Canonical Stage 2 readers and provider-context rematerialization.

The reader never replays ``agent_events``.  It reads canonical relations at a
single repeatable-read cut and lowers incomplete physical effects into
provider-only closure items.  Those closures are not durable facts and never
authorize a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from typing import Mapping, Protocol, Sequence

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    PreparedProviderInputCut,
)
from pulsara_agent.ports.terminal_observation import (
    TerminalDeliveryCoverage,
    TerminalObservationContentV1,
    TerminalObservationKind,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


class ProviderInputItemKind(StrEnum):
    CONTEXT_SNAPSHOT = "CONTEXT_SNAPSHOT"
    USER = "USER"
    TERMINAL_OBSERVATION = "TERMINAL_OBSERVATION"
    ASSISTANT = "ASSISTANT"
    ASSISTANT_TOOL_REQUEST = "ASSISTANT_TOOL_REQUEST"
    TOOL_RESULT = "TOOL_RESULT"
    TOOL_RESULT_CLOSURE = "TOOL_RESULT_CLOSURE"
    LATE_TOOL_OUTCOME = "LATE_TOOL_OUTCOME"


class CanonicalProviderContinuityFailureKind(StrEnum):
    BLOB_READER_UNAVAILABLE = "BLOB_READER_UNAVAILABLE"
    BLOB_UNAVAILABLE_OR_CORRUPT = "BLOB_UNAVAILABLE_OR_CORRUPT"
    CONTENT_SIZE_MISMATCH = "CONTENT_SIZE_MISMATCH"
    CONTENT_DIGEST_MISMATCH = "CONTENT_DIGEST_MISMATCH"
    INVALID_UTF8 = "INVALID_UTF8"


class CanonicalProviderContinuityError(ConversationKernelConflict):
    """Canonical history cannot be admitted to a new provider operation."""

    def __init__(
        self,
        kind: CanonicalProviderContinuityFailureKind,
        *,
        content_identity: str | None = None,
    ) -> None:
        self.kind = kind
        self.content_identity = content_identity
        suffix = "" if content_identity is None else f" ({content_identity})"
        super().__init__(f"canonical provider continuity failed: {kind.value}{suffix}")


class ProviderToolResultClosureKind(StrEnum):
    INTERRUPTED_BEFORE_DISPATCH = "interrupted_before_dispatch"
    INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED = "interrupted_may_have_partially_executed"


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProviderInputItem:
    item_kind: ProviderInputItemKind
    source_entry_id: str | None
    source_entry_sequence: int | None
    text: str
    tool_calls: tuple[ProviderToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderToolResultClosure:
    assistant_entry_id: str
    tool_call_id: str
    closure_kind: ProviderToolResultClosureKind
    target_provider_input_through_sequence: int


@dataclass(frozen=True, slots=True)
class LateToolOutcomeObservation:
    assistant_entry_id: str
    tool_call_id: str
    result_entry_id: str
    result_entry_sequence: int
    result_state: str


@dataclass(frozen=True, slots=True)
class RematerializedProviderInput:
    cut: PreparedProviderInputCut
    conversation_scope_kind: str
    scope_subagent_task_id: str | None
    binding_revision_ordinal: int
    items: tuple[ProviderInputItem, ...]
    closures: tuple[ProviderToolResultClosure, ...]
    late_outcomes: tuple[LateToolOutcomeObservation, ...]
    canonical_bytes: int


class CanonicalBlobReader(Protocol):
    def read_exact(
        self,
        *,
        blob_id: str,
        expected_digest: str,
        expected_size: int,
        deadline_monotonic: float,
    ) -> bytes: ...


class CanonicalProviderInputReader:
    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        blob_reader: CanonicalBlobReader | None = None,
        maximum_items: int = 4096,
        maximum_canonical_bytes: int = 16 << 20,
    ) -> None:
        if maximum_items < 1 or maximum_canonical_bytes < 1:
            raise ValueError("provider input bounds must be positive")
        self._provider = connection_provider
        self._blob_reader = blob_reader
        self._maximum_items = maximum_items
        self._maximum_canonical_bytes = maximum_canonical_bytes

    def rematerialize(
        self,
        cut: PreparedProviderInputCut,
        *,
        deadline_monotonic: float,
    ) -> RematerializedProviderInput:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            binding = connection.execute(
                """
                SELECT t.conversation_scope_kind, t.scope_subagent_task_id,
                       t.status AS turn_status,
                       t.current_context_binding_revision_id,
                       r.revision_ordinal, r.base_kind,
                       r.context_snapshot_id, r.source_through_sequence,
                       s.latest_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.turn_context_binding_revisions AS r
                  ON r.session_id = t.session_id
                 AND r.id = %s
                 AND r.turn_id = t.id
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                WHERE t.session_id = %s AND t.id = %s
                """,
                (
                    cut.context_binding_revision_id,
                    cut.session_id,
                    cut.turn_id,
                ),
            ).fetchone()
            if binding is None:
                raise ConversationKernelConflict("provider binding is absent")
            if (
                binding["current_context_binding_revision_id"]
                != cut.context_binding_revision_id
            ):
                raise ConversationKernelConflict("provider binding revision is stale")
            if cut.provider_input_through_sequence > int(
                binding["latest_entry_sequence"]
            ):
                raise ConversationKernelConflict(
                    "provider input cut exceeds canonical head"
                )

            scope_kind = str(binding["conversation_scope_kind"])
            scope_task_id = binding["scope_subagent_task_id"]
            source_floor = 0
            items: list[ProviderInputItem] = []
            canonical_bytes = 0
            if binding["base_kind"] == "SNAPSHOT":
                snapshot = connection.execute(
                    """
                    SELECT inline_content, blob_id, content_digest, content_size,
                           content_media_type, content_codec, source_through_sequence
                    FROM pulsara_v3.context_snapshots
                    WHERE session_id = %s AND id = %s
                    """,
                    (cut.session_id, binding["context_snapshot_id"]),
                ).fetchone()
                if snapshot is None:
                    raise ConversationKernelConflict("context snapshot is absent")
                source_floor = int(snapshot["source_through_sequence"])
                if source_floor != int(binding["source_through_sequence"]):
                    raise ConversationKernelConflict(
                        "snapshot binding source cut drifted"
                    )
                content = self._read_content(
                    snapshot,
                    deadline_monotonic=deadline_monotonic,
                )
                text = _decode_provider_text(content, str(snapshot["content_codec"]))
                items.append(
                    ProviderInputItem(
                        item_kind=ProviderInputItemKind.CONTEXT_SNAPSHOT,
                        source_entry_id=None,
                        source_entry_sequence=source_floor,
                        text=text,
                    )
                )
                canonical_bytes += len(content)

            entries = connection.execute(
                """
                SELECT e.*, t.status AS owning_turn_status
                FROM pulsara_v3.transcript_entries AS e
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = e.session_id AND t.id = e.turn_id
                WHERE e.session_id = %s
                  AND e.entry_sequence > %s
                  AND e.entry_sequence <= %s
                  AND e.conversation_scope_kind = %s
                  AND e.scope_subagent_task_id IS NOT DISTINCT FROM %s
                ORDER BY e.entry_sequence
                """,
                (
                    cut.session_id,
                    source_floor,
                    cut.provider_input_through_sequence,
                    scope_kind,
                    scope_task_id,
                ),
            ).fetchall()
            if len(entries) > self._maximum_items:
                raise ConversationKernelConflict("provider input item bound exceeded")

            entry_ids = tuple(str(row["id"]) for row in entries)
            blocks_by_entry = self._load_blocks(connection, cut.session_id, entry_ids)
            tool_state = self._load_tool_state(connection, cut.session_id, entry_ids)
            next_assistant_cut = _next_assistant_cuts(entries)
            closures: list[ProviderToolResultClosure] = []
            late: list[LateToolOutcomeObservation] = []
            late_items: list[tuple[int, ProviderInputItem]] = []

            for row in entries:
                entry_id = str(row["id"])
                sequence = int(row["entry_sequence"])
                kind = str(row["entry_kind"])
                if kind == "TOOL_RESULT":
                    continue
                content = self._read_content(
                    row,
                    deadline_monotonic=deadline_monotonic,
                )
                canonical_bytes += len(content)
                text = _decode_provider_text(content, str(row["content_codec"]))
                if kind in ("USER_MESSAGE", "USER_STEER"):
                    items.append(
                        ProviderInputItem(
                            ProviderInputItemKind.USER,
                            entry_id,
                            sequence,
                            text,
                        )
                    )
                    continue
                if kind == "TERMINAL_OBSERVATION":
                    if (
                        str(row["content_media_type"])
                        != "application/vnd.pulsara.terminal-observation+json"
                        or str(row["content_codec"]) != "utf-8"
                    ):
                        raise ConversationKernelConflict(
                            "terminal observation content descriptor is invalid"
                        )
                    _validate_terminal_observation_content(content)
                    items.append(
                        ProviderInputItem(
                            ProviderInputItemKind.TERMINAL_OBSERVATION,
                            entry_id,
                            sequence,
                            text,
                        )
                    )
                    continue
                blocks = blocks_by_entry.get(entry_id, ())
                text_parts = tuple(
                    self._block_text(item, deadline_monotonic=deadline_monotonic)
                    for item in blocks
                    if item["block_kind"] in ("TEXT", "DATA")
                )
                canonical_bytes += sum(len(part.encode("utf-8")) for part in text_parts)
                calls = tuple(
                    ProviderToolCall(
                        tool_call_id=str(item["tool_call_id"]),
                        tool_name=str(item["tool_name"]),
                        arguments=dict(item["tool_arguments"]),
                    )
                    for item in blocks
                    if item["block_kind"] == "TOOL_CALL"
                )
                items.append(
                    ProviderInputItem(
                        (
                            ProviderInputItemKind.ASSISTANT_TOOL_REQUEST
                            if calls
                            else ProviderInputItemKind.ASSISTANT
                        ),
                        entry_id,
                        sequence,
                        "".join(text_parts) or text,
                        calls,
                    )
                )
                if not calls:
                    continue
                target_cut = next_assistant_cut.get(entry_id)
                if target_cut is None and row["owning_turn_status"] == "RUNNING":
                    target_cut = cut.provider_input_through_sequence
                if target_cut is None:
                    target_cut = sequence
                for call in calls:
                    state = tool_state.get((entry_id, call.tool_call_id), {})
                    result = state.get("result")
                    result_sequence = (
                        None if result is None else int(result["entry_sequence"])
                    )
                    if result is not None and result_sequence <= target_cut:
                        result_content = self._read_content(
                            result,
                            deadline_monotonic=deadline_monotonic,
                        )
                        canonical_bytes += len(result_content)
                        items.append(
                            ProviderInputItem(
                                ProviderInputItemKind.TOOL_RESULT,
                                str(result["result_entry_id"]),
                                result_sequence,
                                _decode_provider_text(
                                    result_content, str(result["content_codec"])
                                ),
                                tool_call_id=call.tool_call_id,
                            )
                        )
                        continue
                    closure_kind = (
                        ProviderToolResultClosureKind.INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED
                        if state.get("attempt_id") is not None
                        else ProviderToolResultClosureKind.INTERRUPTED_BEFORE_DISPATCH
                    )
                    closure = ProviderToolResultClosure(
                        assistant_entry_id=entry_id,
                        tool_call_id=call.tool_call_id,
                        closure_kind=closure_kind,
                        target_provider_input_through_sequence=target_cut,
                    )
                    closures.append(closure)
                    closure_text = _canonical_json_text(
                        {
                            "schema_version": "provider_tool_result_closure.v1",
                            "tool_call_id": call.tool_call_id,
                            "disposition": closure_kind.value,
                        }
                    )
                    canonical_bytes += len(closure_text.encode("utf-8"))
                    items.append(
                        ProviderInputItem(
                            ProviderInputItemKind.TOOL_RESULT_CLOSURE,
                            None,
                            sequence,
                            closure_text,
                            tool_call_id=call.tool_call_id,
                        )
                    )
                    if (
                        result is not None
                        and result_sequence <= cut.provider_input_through_sequence
                    ):
                        observation = LateToolOutcomeObservation(
                            assistant_entry_id=entry_id,
                            tool_call_id=call.tool_call_id,
                            result_entry_id=str(result["result_entry_id"]),
                            result_entry_sequence=result_sequence,
                            result_state=str(result["result_state"]),
                        )
                        late.append(observation)
                        result_content = self._read_content(
                            result,
                            deadline_monotonic=deadline_monotonic,
                        )
                        late_text = _canonical_json_text(
                            {
                                "schema_version": "late_tool_outcome_observation.v1",
                                "tool_call_id": call.tool_call_id,
                                "result_state": result["result_state"],
                                "result": _decode_provider_text(
                                    result_content, str(result["content_codec"])
                                ),
                            }
                        )
                        canonical_bytes += len(late_text.encode("utf-8"))
                        late_items.append(
                            (
                                result_sequence,
                                ProviderInputItem(
                                    ProviderInputItemKind.LATE_TOOL_OUTCOME,
                                    str(result["result_entry_id"]),
                                    result_sequence,
                                    late_text,
                                    tool_call_id=call.tool_call_id,
                                ),
                            )
                        )

            # Late outcomes retain their real canonical sequence but never
            # replace the closure paired with the historical request.
            for late_sequence, late_item in sorted(
                late_items, key=lambda item: item[0]
            ):
                insert_at = len(items)
                for index, item in enumerate(items):
                    if (
                        item.source_entry_sequence is not None
                        and item.source_entry_sequence > late_sequence
                    ):
                        insert_at = index
                        break
                items.insert(insert_at, late_item)
            if len(items) > self._maximum_items:
                raise ConversationKernelConflict("provider input item bound exceeded")
            if canonical_bytes > self._maximum_canonical_bytes:
                raise ConversationKernelConflict("provider input byte bound exceeded")
            return RematerializedProviderInput(
                cut=cut,
                conversation_scope_kind=scope_kind,
                scope_subagent_task_id=scope_task_id,
                binding_revision_ordinal=int(binding["revision_ordinal"]),
                items=tuple(items),
                closures=tuple(closures),
                late_outcomes=tuple(late),
                canonical_bytes=canonical_bytes,
            )

    @staticmethod
    def _load_blocks(connection, session_id: str, entry_ids: Sequence[str]):
        result: dict[str, list[Mapping[str, object]]] = {}
        if not entry_ids:
            return result
        for row in connection.execute(
            """
            SELECT * FROM pulsara_v3.assistant_message_blocks
            WHERE session_id = %s AND assistant_entry_id = ANY(%s)
            ORDER BY assistant_entry_id, block_ordinal
            """,
            (session_id, list(entry_ids)),
        ).fetchall():
            result.setdefault(str(row["assistant_entry_id"]), []).append(row)
        return {key: tuple(value) for key, value in result.items()}

    @staticmethod
    def _load_tool_state(connection, session_id: str, entry_ids: Sequence[str]):
        state: dict[tuple[str, str], dict[str, object]] = {}
        if not entry_ids:
            return state
        for row in connection.execute(
            """
            SELECT b.assistant_entry_id, b.tool_call_id, a.id AS attempt_id,
                   r.result_state, r.result_entry_id,
                   e.entry_sequence, e.inline_content, e.blob_id,
                   e.content_digest, e.content_size,
                   e.content_media_type, e.content_codec
            FROM pulsara_v3.assistant_message_blocks AS b
            LEFT JOIN pulsara_v3.tool_execution_attempts AS a
              ON a.session_id = b.session_id
             AND a.assistant_entry_id = b.assistant_entry_id
             AND a.tool_call_id = b.tool_call_id
            LEFT JOIN pulsara_v3.tool_results AS r
              ON r.session_id = b.session_id
             AND r.tool_call_entry_id = b.assistant_entry_id
             AND r.tool_call_id = b.tool_call_id
            LEFT JOIN pulsara_v3.transcript_entries AS e
              ON e.session_id = r.session_id AND e.id = r.result_entry_id
            WHERE b.session_id = %s
              AND b.assistant_entry_id = ANY(%s)
              AND b.block_kind = 'TOOL_CALL'
            """,
            (session_id, list(entry_ids)),
        ).fetchall():
            payload: dict[str, object] = {"attempt_id": row["attempt_id"]}
            if row["result_entry_id"] is not None:
                payload["result"] = row
            state[(str(row["assistant_entry_id"]), str(row["tool_call_id"]))] = payload
        return state

    def _read_content(
        self,
        row: Mapping[str, object],
        *,
        deadline_monotonic: float,
    ) -> bytes:
        expected_size = int(row["content_size"])
        expected_digest = str(row["content_digest"])
        if row["inline_content"] is not None:
            content = bytes(row["inline_content"])
        else:
            if self._blob_reader is None:
                raise CanonicalProviderContinuityError(
                    CanonicalProviderContinuityFailureKind.BLOB_READER_UNAVAILABLE,
                    content_identity=str(row.get("blob_id")),
                )
            try:
                content = self._blob_reader.read_exact(
                    blob_id=str(row["blob_id"]),
                    expected_digest=expected_digest,
                    expected_size=expected_size,
                    deadline_monotonic=deadline_monotonic,
                )
            except CanonicalProviderContinuityError:
                raise
            except Exception as exc:
                raise CanonicalProviderContinuityError(
                    CanonicalProviderContinuityFailureKind.BLOB_UNAVAILABLE_OR_CORRUPT,
                    content_identity=str(row.get("blob_id")),
                ) from exc
        if len(content) != expected_size:
            raise CanonicalProviderContinuityError(
                CanonicalProviderContinuityFailureKind.CONTENT_SIZE_MISMATCH,
                content_identity=expected_digest,
            )
        if "sha256:" + sha256(content).hexdigest() != expected_digest:
            raise CanonicalProviderContinuityError(
                CanonicalProviderContinuityFailureKind.CONTENT_DIGEST_MISMATCH,
                content_identity=expected_digest,
            )
        return content

    def _block_text(
        self,
        row: Mapping[str, object],
        *,
        deadline_monotonic: float,
    ) -> str:
        return _decode_provider_text(
            self._read_content(row, deadline_monotonic=deadline_monotonic),
            str(row["content_codec"]),
        )


def _next_assistant_cuts(
    entries: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    assistants = [
        row
        for row in entries
        if row["entry_kind"] in ("ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST")
    ]
    result: dict[str, int] = {}
    for index, row in enumerate(assistants[:-1]):
        next_row = assistants[index + 1]
        result[str(row["id"])] = int(next_row["provider_input_through_sequence"])
    return result


def _decode_provider_text(content: bytes, codec: str) -> str:
    if codec != "utf-8":
        return _canonical_json_text(
            {
                "schema_version": "canonical_binary_content.v1",
                "codec": codec,
                "size": len(content),
                "digest": "sha256:" + sha256(content).hexdigest(),
            }
        )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalProviderContinuityError(
            CanonicalProviderContinuityFailureKind.INVALID_UTF8,
            content_identity="sha256:" + sha256(content).hexdigest(),
        ) from exc


def _canonical_json_text(payload: Mapping[str, object]) -> str:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _validate_terminal_observation_content(content: bytes) -> None:
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "observation_id",
            "monitor_id",
            "process_id",
            "observation_ordinal",
            "observation_kind",
            "process_status",
            "exit_code",
            "output_disposition",
            "gap_before_output",
            "delivery_coverage",
            "available_source_utf8_bytes",
            "included_source_utf8_bytes",
            "omitted_by_delivery_bound_utf8_bytes",
            "output",
            "host_scoped",
        }:
            raise ValueError("terminal observation schema is not closed")
        integer_fields = (
            "observation_ordinal",
            "available_source_utf8_bytes",
            "included_source_utf8_bytes",
            "omitted_by_delivery_bound_utf8_bytes",
        )
        if any(
            not isinstance(payload[field], int)
            or isinstance(payload[field], bool)
            for field in integer_fields
        ):
            raise ValueError("terminal observation integer field is invalid")
        if not isinstance(payload["gap_before_output"], bool) or not isinstance(
            payload["host_scoped"], bool
        ):
            raise ValueError("terminal observation boolean field is invalid")
        if payload["exit_code"] is not None and (
            not isinstance(payload["exit_code"], int)
            or isinstance(payload["exit_code"], bool)
        ):
            raise ValueError("terminal observation exit code is invalid")
        if any(
            not isinstance(payload[field], str)
            for field in (
                "schema_version",
                "observation_id",
                "monitor_id",
                "process_id",
                "observation_kind",
                "process_status",
                "output_disposition",
                "delivery_coverage",
                "output",
            )
        ):
            raise ValueError("terminal observation string field is invalid")
        fact = TerminalObservationContentV1(
            schema_version=str(payload["schema_version"]),
            observation_id=str(payload["observation_id"]),
            monitor_id=str(payload["monitor_id"]),
            process_id=str(payload["process_id"]),
            observation_ordinal=int(payload["observation_ordinal"]),
            observation_kind=TerminalObservationKind(
                str(payload["observation_kind"])
            ),
            process_status=str(payload["process_status"]),
            exit_code=(
                None if payload["exit_code"] is None else int(payload["exit_code"])
            ),
            output_disposition=str(payload["output_disposition"]),
            gap_before_output=payload["gap_before_output"],
            delivery_coverage=TerminalDeliveryCoverage(
                str(payload["delivery_coverage"])
            ),
            available_source_utf8_bytes=int(
                payload["available_source_utf8_bytes"]
            ),
            included_source_utf8_bytes=int(payload["included_source_utf8_bytes"]),
            omitted_by_delivery_bound_utf8_bytes=int(
                payload["omitted_by_delivery_bound_utf8_bytes"]
            ),
            output=str(payload["output"]),
            host_scoped=payload["host_scoped"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConversationKernelConflict(
            "terminal observation canonical envelope is invalid"
        ) from exc
    if fact.canonical_bytes() != content:
        raise ConversationKernelConflict(
            "terminal observation canonical encoding is not unique"
        )


__all__ = [
    "CanonicalBlobReader",
    "CanonicalProviderInputReader",
    "CanonicalProviderContinuityError",
    "CanonicalProviderContinuityFailureKind",
    "LateToolOutcomeObservation",
    "ProviderInputItem",
    "ProviderInputItemKind",
    "ProviderToolCall",
    "ProviderToolResultClosure",
    "ProviderToolResultClosureKind",
    "RematerializedProviderInput",
]
