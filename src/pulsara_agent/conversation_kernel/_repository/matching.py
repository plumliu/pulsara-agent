"""Pure row and confirmation matching operations."""

from __future__ import annotations

from typing import Mapping
from hashlib import sha256
from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    CommittedEventDraft,
    EntryKind,
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.vocabulary import SubjectSlot
from pulsara_agent.model_input.contracts import CanonicalInputOriginKind
from pulsara_agent.conversation_kernel.steer import (
    PreparedSteerConsumptionCandidate,
    PreparedSteerResourceRejection,
)

from .contracts import (
    ConversationKernelConflict,
)


class _MatchingOperations:
    @staticmethod
    def _content_from_row(connection, row: Mapping[str, object]) -> CanonicalContent:
        from pulsara_agent.conversation_kernel.content_parts import read_content_parts
        from .queue_content import read_input_owner, input_body_storage

        if (
            row.get("entry_kind")
            in {"USER_MESSAGE", "USER_STEER", "TERMINAL_OBSERVATION"}
            or "queue_sequence" in row
        ):
            value = read_input_owner(
                connection,
                row=row,
                queue_item_id=str(row["id"]) if "queue_sequence" in row else None,
                transcript_entry_id=str(row["id"]) if "entry_sequence" in row else None,
            )
            return input_body_storage(value)
        parts = read_content_parts(
            connection, session_id=str(row["session_id"]), entry_id=str(row["id"])
        )
        if len(parts) == 1 and parts[0].type == "data":
            body, media_type, codec = parts[0].value, parts[0].data_media_type, parts[0].data_codec
        elif all(part.type == "text" for part in parts):
            body, media_type, codec = (
                "".join(part.value for part in parts).encode("utf-8"),
                "text/plain", "utf-8",
            )
        else:
            raise ConversationKernelConflict(
                "content does not have a single canonical byte projection"
            )
        return InlineContent(
            canonical_bytes=body,
            digest="sha256:" + sha256(body).hexdigest(),
            size=len(body),
            media_type=media_type,
            codec=codec,
        )


def _prompt_steer_row_matches_candidate(
    row: Mapping[str, object] | None, candidate: PreparedSteerConsumptionCandidate
) -> bool:
    if row is None:
        return False
    return bool(
        str(row["id"]) == candidate.queue_item_id
        and int(row["queue_sequence"]) == candidate.queue_sequence
        and row["command_id"] == candidate.command_id
        and str(row["delivery_mode"]) == PromptDeliveryMode.STEER_ACTIVE_TURN.value
        and row["input_origin"] == candidate.input_origin.value
        and str(row["target_turn_id"]) == candidate.exact_target_turn_id
    )


def _prompt_steer_row_matches_resource_rejection(
    row: Mapping[str, object] | None,
    candidate: PreparedSteerResourceRejection,
) -> bool:
    if row is None:
        return False
    return bool(
        str(row["delivery_mode"]) == PromptDeliveryMode.STEER_ACTIVE_TURN.value
        and str(row["session_id"]) == candidate.session_id
        and str(row["workspace_id"]) == candidate.workspace_id
        and str(row["id"]) == candidate.queue_item_id
        and int(row["queue_sequence"]) == candidate.queue_sequence
        and row["command_id"] == candidate.command_id
        and str(row["target_turn_id"]) == candidate.exact_target_turn_id
    )


def _accepted_steer_entry_matches(
    row: Mapping[str, object] | None,
    candidate: PreparedSteerConsumptionCandidate,
) -> bool:
    if row is None:
        return False
    return bool(
        str(row["id"]) == candidate.new_entry_id
        and str(row["turn_id"]) == candidate.exact_target_turn_id
        and int(row["entry_sequence"]) == candidate.expected_entry_sequence
        and str(row["entry_kind"])
        == (
            EntryKind.TERMINAL_OBSERVATION.value
            if candidate.input_origin is CanonicalInputOriginKind.CELL_OUTPUT
            else EntryKind.USER_STEER.value
        )
        and row["input_origin"] == candidate.input_origin.value
    )


def _event_row_matches_draft(
    row: Mapping[str, object] | None, draft: CommittedEventDraft
) -> bool:
    if row is None:
        return False
    subject_matches = bool(
        row.get(draft.subject.slot.value) == draft.subject.subject_id
        and all(
            row.get(slot.value) is None
            for slot in SubjectSlot
            if slot is not draft.subject.slot
        )
    )
    return bool(
        str(row["event_id"]) == draft.event_id
        and str(row["event_type"]) == draft.event_type.value
        and row["occurred_at"] == draft.occurred_at
        and str(row["actor_kind"]) == draft.actor_kind
        and str(row["actor_id"]) == draft.actor_id
        and str(row["sensitivity_class"]) == draft.sensitivity_class
        and str(row["projection_profile"]) == draft.projection_profile
        and dict(row["payload"]) == dict(draft.payload)
        and subject_matches
    )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConversationKernelConflict(f"{field} is missing")
    return value


def _required_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConversationKernelConflict(f"{field} is invalid")
    return value
