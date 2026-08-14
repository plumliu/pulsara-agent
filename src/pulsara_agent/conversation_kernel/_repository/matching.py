"""Pure row and confirmation matching operations."""

from __future__ import annotations

from typing import Mapping
from pulsara_agent.conversation_kernel.contracts import BlobContent, CanonicalContent, CommittedEventDraft, ConversationScopeKind, EntryKind, InlineContent, PromptDeliveryMode
from pulsara_agent.conversation_kernel.vocabulary import SubjectSlot
from pulsara_agent.conversation_kernel.steer import PreparedSteerConsumptionCandidate, PreparedSteerResourceRejection, build_pending_prompt_steer_fact

from .contracts import (
    ConversationKernelConflict,
)

class _MatchingOperations:
    @staticmethod
    def _content_from_row(row: Mapping[str, object]) -> CanonicalContent:
        if row["inline_content"] is not None:
            return InlineContent(
                canonical_bytes=bytes(row["inline_content"]),
                digest=str(row["content_digest"]),
                size=int(row["content_size"]),
                media_type=str(row["content_media_type"]),
                codec=str(row["content_codec"]),
            )
        return BlobContent(
            blob_id=str(row["blob_id"]),
            digest=str(row["content_digest"]),
            size=int(row["content_size"]),
            media_type=str(row["content_media_type"]),
            codec=str(row["content_codec"]),
        )



def _prompt_steer_row_matches_candidate(
    row: Mapping[str, object] | None, candidate: PreparedSteerConsumptionCandidate
) -> bool:
    if row is None:
        return False
    content = candidate.content
    storage_matches = (
        (
            bytes(row["inline_content"]) == content.canonical_bytes
            and row["blob_id"] is None
        )
        if isinstance(content, InlineContent)
        else (row["inline_content"] is None and str(row["blob_id"]) == content.blob_id)
    )
    return bool(
        str(row["id"]) == candidate.queue_item_id
        and int(row["queue_sequence"]) == candidate.queue_sequence
        and str(row["command_id"]) == candidate.command_id
        and str(row["delivery_mode"]) == PromptDeliveryMode.STEER_ACTIVE_TURN.value
        and str(row["target_turn_id"]) == candidate.exact_target_turn_id
        and str(row["content_digest"]) == content.digest
        and int(row["content_size"]) == content.size
        and str(row["content_media_type"]) == content.media_type
        and str(row["content_codec"]) == content.codec
        and storage_matches
    )


def _prompt_steer_row_matches_resource_rejection(
    row: Mapping[str, object] | None,
    candidate: PreparedSteerResourceRejection,
) -> bool:
    if row is None:
        return False
    try:
        fact = build_pending_prompt_steer_fact(
            session_id=str(row["session_id"]),
            workspace_id=str(row["workspace_id"]),
            queue_item_id=str(row["id"]),
            queue_sequence=int(row["queue_sequence"]),
            command_id=str(row["command_id"]),
            exact_target_turn_id=str(row["target_turn_id"]),
            content=_MatchingOperations._content_from_row(row),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        str(row["delivery_mode"]) == PromptDeliveryMode.STEER_ACTIVE_TURN.value
        and fact.fact_fingerprint == candidate.expected_pending_fact_fingerprint
        and fact.session_id == candidate.session_id
        and fact.workspace_id == candidate.workspace_id
        and fact.queue_item_id == candidate.queue_item_id
        and fact.queue_sequence == candidate.queue_sequence
        and fact.command_id == candidate.command_id
        and fact.exact_target_turn_id == candidate.exact_target_turn_id
        and fact.content == candidate.content
    )


def _accepted_steer_entry_matches(
    row: Mapping[str, object] | None,
    candidate: PreparedSteerConsumptionCandidate,
) -> bool:
    if row is None:
        return False
    content = candidate.content
    storage_matches = (
        (
            bytes(row["inline_content"]) == content.canonical_bytes
            and row["blob_id"] is None
        )
        if isinstance(content, InlineContent)
        else (row["inline_content"] is None and str(row["blob_id"]) == content.blob_id)
    )
    return bool(
        str(row["id"]) == candidate.new_entry_id
        and str(row["turn_id"]) == candidate.exact_target_turn_id
        and int(row["entry_sequence"]) == candidate.expected_entry_sequence
        and str(row["entry_kind"]) == EntryKind.USER_STEER.value
        and str(row["conversation_scope_kind"]) == ConversationScopeKind.ROOT.value
        and row["scope_subagent_task_id"] is None
        and str(row["content_digest"]) == content.digest
        and int(row["content_size"]) == content.size
        and str(row["content_media_type"]) == content.media_type
        and str(row["content_codec"]) == content.codec
        and storage_matches
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
    expected_child_kind: str | None = None
    if draft.subject.slot is SubjectSlot.SUBAGENT_MESSAGE:
        expected_child_kind = "MESSAGE"
    elif draft.subject.slot is SubjectSlot.SUBAGENT_RESULT:
        expected_child_kind = "RESULT"
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
        and row.get("subject_subagent_child_kind") == expected_child_kind
    )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConversationKernelConflict(f"{field} is missing")
    return value


def _required_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConversationKernelConflict(f"{field} is invalid")
    return value
