"""Closed value contracts for the Stage 2 canonical kernel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Mapping

from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.conversation_kernel.vocabulary import (
    AppendGuardKind,
    CommittedEventType,
    SubjectSlot,
)


def _required(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def canonical_digest(namespace: str, value: object) -> str:
    _required(namespace, "namespace")
    return (
        "sha256:"
        + sha256(
            namespace.encode("utf-8") + b"\0" + canonical_json_bytes(value)
        ).hexdigest()
    )


class SessionLifecycle(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TurnStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"


class HostWriterAcquisitionKind(StrEnum):
    NEW_SESSION = "NEW_SESSION"
    SAME_OWNER_RENEWAL = "SAME_OWNER_RENEWAL"
    HOST_TAKEOVER = "HOST_TAKEOVER"


class ConversationScopeKind(StrEnum):
    ROOT = "ROOT"
    SUBAGENT_TASK = "SUBAGENT_TASK"


class EntryKind(StrEnum):
    USER_MESSAGE = "USER_MESSAGE"
    USER_STEER = "USER_STEER"
    TERMINAL_OBSERVATION = "TERMINAL_OBSERVATION"
    USER_CONTROL_FEEDBACK = "USER_CONTROL_FEEDBACK"
    ASSISTANT_MESSAGE = "ASSISTANT_MESSAGE"
    ASSISTANT_TOOL_REQUEST = "ASSISTANT_TOOL_REQUEST"
    TOOL_RESULT = "TOOL_RESULT"
    PLAN_CONTINUATION = "PLAN_CONTINUATION"
    INTER_AGENT_MESSAGE = "INTER_AGENT_MESSAGE"


class AssistantBlockKind(StrEnum):
    TEXT = "TEXT"
    DATA = "DATA"
    TOOL_CALL = "TOOL_CALL"


class PromptDeliveryMode(StrEnum):
    NEW_TURN = "NEW_TURN"
    STEER_ACTIVE_TURN = "STEER_ACTIVE_TURN"


@dataclass(frozen=True, slots=True)
class HostWriterGuard:
    session_id: str
    writer_generation: int
    writer_owner_id: str

    def __post_init__(self) -> None:
        _required(self.session_id, "session_id")
        _required(self.writer_owner_id, "writer_owner_id")
        if self.writer_generation < 1:
            raise ValueError("writer_generation must be positive")

    @property
    def kind(self) -> AppendGuardKind:
        return AppendGuardKind.HOST_WRITER


AppendGuard = HostWriterGuard


@dataclass(frozen=True, slots=True)
class CommittedEventSubject:
    slot: SubjectSlot
    subject_id: str

    def __post_init__(self) -> None:
        _required(self.subject_id, "subject_id")


@dataclass(frozen=True, slots=True)
class CommittedEventDraft:
    event_id: str
    event_type: CommittedEventType
    subject: CommittedEventSubject
    actor_kind: str
    actor_id: str
    sensitivity_class: str
    projection_profile: str
    occurred_at: datetime
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        for field, value in (
            ("event_id", self.event_id),
            ("actor_kind", self.actor_kind),
            ("actor_id", self.actor_id),
            ("sensitivity_class", self.sensitivity_class),
            ("projection_profile", self.projection_profile),
        ):
            _required(value, field)
        encoded = canonical_json_bytes(dict(self.payload))
        if len(encoded) > 64 * 1024:
            raise ValueError("committed event payload exceeds 64 KiB")


@dataclass(frozen=True, slots=True)
class StoredCommittedEvent:
    event_id: str
    workspace_id: str
    session_id: str
    event_sequence: int
    event_type: CommittedEventType
    subject: CommittedEventSubject
    accepted_at: datetime
    occurred_at: datetime
    actor_kind: str
    actor_id: str
    sensitivity_class: str
    projection_profile: str
    payload: Mapping[str, object]
    # Process-local attribution resolved from the canonical transaction.  It
    # is not another durable subject slot and never participates in replay.
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class InlineContent:
    canonical_bytes: bytes
    digest: str
    size: int
    media_type: str
    codec: str

    @classmethod
    def from_bytes(
        cls, data: bytes, *, media_type: str = "text/plain", codec: str = "utf-8"
    ) -> "InlineContent":
        if len(data) > 64 * 1024:
            raise ValueError("inline content exceeds 64 KiB")
        return cls(
            canonical_bytes=bytes(data),
            digest="sha256:" + sha256(data).hexdigest(),
            size=len(data),
            media_type=_required(media_type, "media_type"),
            codec=_required(codec, "codec"),
        )

    def __post_init__(self) -> None:
        if self.size != len(self.canonical_bytes):
            raise ValueError("inline content size mismatch")
        if self.digest != "sha256:" + sha256(self.canonical_bytes).hexdigest():
            raise ValueError("inline content digest mismatch")


@dataclass(frozen=True, slots=True)
class BlobContent:
    blob_id: str
    digest: str
    size: int
    media_type: str
    codec: str

    def __post_init__(self) -> None:
        _required(self.blob_id, "blob_id")
        if self.size < 0:
            raise ValueError("blob size must be non-negative")


CanonicalContent = InlineContent | BlobContent


@dataclass(frozen=True, slots=True)
class WriterLease:
    guard: HostWriterGuard
    expires_at: datetime
    acquisition_kind: HostWriterAcquisitionKind

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            raise ValueError("writer lease expiry must be timezone-aware")
        if not isinstance(self.acquisition_kind, HostWriterAcquisitionKind):
            raise TypeError("writer lease acquisition kind must be closed")
        if (
            self.acquisition_kind is HostWriterAcquisitionKind.NEW_SESSION
            and self.guard.writer_generation != 1
        ):
            raise ValueError("new-session writer lease must start at generation one")


@dataclass(frozen=True, slots=True)
class CanonicalSessionSnapshot:
    session_id: str
    workspace_id: str
    lifecycle: SessionLifecycle
    writer_generation: int
    entry_sequence_cut: int
    event_sequence_cut: int
    prompt_queue_sequence_cut: int
    entries: tuple[Mapping[str, object], ...]
    active_turns: tuple[Mapping[str, object], ...]
    prompt_queue: tuple[Mapping[str, object], ...]
    tool_control: tuple[Mapping[str, object], ...]
    memory_freshness: tuple[Mapping[str, object], ...]


__all__ = [
    "AppendGuard",
    "AssistantBlockKind",
    "BlobContent",
    "CanonicalContent",
    "CanonicalSessionSnapshot",
    "CommittedEventDraft",
    "CommittedEventSubject",
    "ConversationScopeKind",
    "EntryKind",
    "HostWriterAcquisitionKind",
    "HostWriterGuard",
    "InlineContent",
    "PromptDeliveryMode",
    "SessionLifecycle",
    "StoredCommittedEvent",
    "TurnStatus",
    "WriterLease",
    "canonical_digest",
]
