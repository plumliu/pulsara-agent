"""Renderer-neutral payload vocabulary for the exact 24 live events.

This module is the final type owner shared by provider adapters, the Runtime
live bus, hooks, and Protocol v3.  Keeping these values in ``ports`` prevents
the adapter boundary from inventing a second per-delta vocabulary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Mapping, TypeAlias, TypeGuard
import unicodedata

from pulsara_agent.primitives.todo import (
    MAXIMUM_TODO_CANONICAL_JSON_BYTES,
    MAXIMUM_TODO_ITEMS,
    MAXIMUM_TODO_TEXT_UTF8_BYTES,
    todo_snapshot_canonical_json,
)


def live_digest(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _require_identity(*values: str) -> None:
    if not values or any(not value for value in values):
        raise ValueError("live payload identity is empty")


def _validate_frozen_text(value: str, utf8_bytes: int, digest: str) -> None:
    encoded = value.encode("utf-8")
    if utf8_bytes != len(encoded) or digest != live_digest(value):
        raise ValueError("live terminal payload integrity mismatch")


@dataclass(frozen=True, slots=True)
class TextStartPayload:
    block_identity: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)


@dataclass(frozen=True, slots=True)
class TextDeltaPayload:
    block_identity: str
    delta: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)
        if not self.delta:
            raise ValueError("text delta is empty")


@dataclass(frozen=True, slots=True)
class TextEndPayload:
    block_identity: str
    final_text: str
    utf8_bytes: int
    digest: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)
        _validate_frozen_text(self.final_text, self.utf8_bytes, self.digest)


@dataclass(frozen=True, slots=True)
class ThinkingStartPayload:
    block_identity: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)


@dataclass(frozen=True, slots=True)
class ThinkingDeltaPayload:
    block_identity: str
    delta: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)
        if not self.delta:
            raise ValueError("thinking delta is empty")


@dataclass(frozen=True, slots=True)
class ThinkingEndPayload:
    block_identity: str
    final_text: str
    utf8_bytes: int
    digest: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)
        _validate_frozen_text(self.final_text, self.utf8_bytes, self.digest)


@dataclass(frozen=True, slots=True)
class DataStartPayload:
    block_identity: str
    media_type: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity, self.media_type)


@dataclass(frozen=True, slots=True)
class DataDeltaPayload:
    block_identity: str
    data: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)
        if not self.data:
            raise ValueError("data delta is empty")


@dataclass(frozen=True, slots=True)
class DataEndPayload:
    block_identity: str
    media_type: str
    final_data: str
    utf8_bytes: int
    digest: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity, self.media_type)
        _validate_frozen_text(self.final_data, self.utf8_bytes, self.digest)


@dataclass(frozen=True, slots=True)
class ToolCallStartPayload:
    block_identity: str
    tool_call_id: str
    tool_name: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity, self.tool_call_id, self.tool_name)


@dataclass(frozen=True, slots=True)
class ToolCallDeltaPayload:
    block_identity: str
    tool_call_id: str
    delta: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity, self.tool_call_id)
        if not self.delta:
            raise ValueError("tool-call delta is empty")


@dataclass(frozen=True, slots=True)
class ToolCallEndPayload:
    block_identity: str
    tool_call_id: str
    tool_name: str
    arguments_json: str
    utf8_bytes: int
    digest: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity, self.tool_call_id, self.tool_name)
        _validate_frozen_text(self.arguments_json, self.utf8_bytes, self.digest)
        value = json.loads(self.arguments_json or "{}")
        if not isinstance(value, dict):
            raise ValueError("tool-call terminal arguments are not an object")


@dataclass(frozen=True, slots=True)
class ToolResultStartPayload:
    block_identity: str
    tool_call_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity, self.tool_call_id, self.attempt_id)


@dataclass(frozen=True, slots=True)
class ToolResultDeltaPayload:
    block_identity: str
    text: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity)
        if not self.text:
            raise ValueError("tool-result delta is empty")


@dataclass(frozen=True, slots=True)
class ToolResultEndPayload:
    block_identity: str
    result_state: str
    final_text: str
    utf8_bytes: int
    digest: str

    def __post_init__(self) -> None:
        _require_identity(self.block_identity, self.result_state)
        _validate_frozen_text(self.final_text, self.utf8_bytes, self.digest)


@dataclass(frozen=True, slots=True)
class InteractionOpenedPayload:
    interaction_id: str
    interaction_kind: str
    public_prompt: str
    public_options: tuple[str, ...]
    expires_at_utc: str

    def __post_init__(self) -> None:
        _require_identity(
            self.interaction_id,
            self.interaction_kind,
            self.public_prompt,
            self.expires_at_utc,
        )


@dataclass(frozen=True, slots=True)
class InteractionReplacedPayload:
    replaced_interaction_id: str
    interaction_id: str
    interaction_kind: str
    public_prompt: str
    public_options: tuple[str, ...]
    expires_at_utc: str

    def __post_init__(self) -> None:
        _require_identity(
            self.replaced_interaction_id,
            self.interaction_id,
            self.interaction_kind,
            self.public_prompt,
            self.expires_at_utc,
        )


@dataclass(frozen=True, slots=True)
class InteractionClosedPayload:
    interaction_id: str
    reason: str

    def __post_init__(self) -> None:
        _require_identity(self.interaction_id, self.reason)


@dataclass(frozen=True, slots=True)
class TerminalProcessCompletedPayload:
    process_id: str
    status: str
    exit_code: int | None
    output_utf8_bytes: int
    output_digest: str

    def __post_init__(self) -> None:
        _require_identity(self.process_id, self.status, self.output_digest)
        if self.output_utf8_bytes < 0:
            raise ValueError("terminal output byte count is negative")


@dataclass(frozen=True, slots=True)
class TerminalMonitorOpenedPayload:
    monitor_id: str
    process_id: str

    def __post_init__(self) -> None:
        _require_identity(self.monitor_id, self.process_id)


@dataclass(frozen=True, slots=True)
class TerminalMonitorObservationPayload:
    monitor_id: str
    process_id: str
    observation_kind: str
    public_preview: str
    complete_utf8_bytes: int
    complete_digest: str

    def __post_init__(self) -> None:
        _require_identity(
            self.monitor_id,
            self.process_id,
            self.observation_kind,
            self.complete_digest,
        )
        if self.complete_utf8_bytes < len(self.public_preview.encode("utf-8")):
            raise ValueError("terminal preview exceeds complete output")


@dataclass(frozen=True, slots=True)
class TerminalMonitorClosedPayload:
    monitor_id: str
    process_id: str
    reason: str

    def __post_init__(self) -> None:
        _require_identity(self.monitor_id, self.process_id, self.reason)


@dataclass(frozen=True, slots=True)
class SubagentProgressPayload:
    task_id: str
    status: str
    public_summary: str
    summary_utf8_bytes: int
    summary_digest: str

    def __post_init__(self) -> None:
        _require_identity(self.task_id, self.status, self.summary_digest)
        _validate_frozen_text(
            self.public_summary, self.summary_utf8_bytes, self.summary_digest
        )


@dataclass(frozen=True, slots=True)
class TodoLiveItemProjection:
    ordinal: int
    text: str
    status: str

    def __post_init__(self) -> None:
        encoded = self.text.encode("utf-8")
        if (
            self.ordinal < 0
            or not encoded
            or len(encoded) > MAXIMUM_TODO_TEXT_UTF8_BYTES
            or self.text != self.text.strip()
            or self.text != unicodedata.normalize("NFC", self.text)
            or self.status not in {"pending", "in_progress", "completed"}
            or any(
                character in {"\r", "\n", "\u2028", "\u2029"}
                or ord(character) < 0x20
                or 0x7F <= ord(character) <= 0x9F
                for character in self.text
            )
        ):
            raise ValueError("TODO live item is invalid")


@dataclass(frozen=True, slots=True)
class TodoSnapshotUpdatedPayload:
    todo_run_id: str
    todo_revision: int
    disposition: str
    ordered_items: tuple[TodoLiveItemProjection, ...]
    pending_count: int
    in_progress_count: int
    completed_count: int

    def __post_init__(self) -> None:
        _require_identity(self.todo_run_id, self.disposition)
        if self.todo_revision < 0 or self.disposition not in {
            "ACTIVE",
            "CLEARED",
            "CLOSED",
        }:
            raise ValueError("TODO live snapshot identity is invalid")
        if len(self.ordered_items) > MAXIMUM_TODO_ITEMS or tuple(
            item.ordinal for item in self.ordered_items
        ) != tuple(range(len(self.ordered_items))):
            raise ValueError("TODO live item ordering is invalid")
        if len({item.text for item in self.ordered_items}) != len(
            self.ordered_items
        ):
            raise ValueError("TODO live snapshot contains duplicate text")
        actual = {
            status: sum(item.status == status for item in self.ordered_items)
            for status in ("pending", "in_progress", "completed")
        }
        if (
            self.pending_count != actual["pending"]
            or self.in_progress_count != actual["in_progress"]
            or self.completed_count != actual["completed"]
            or self.in_progress_count > 1
        ):
            raise ValueError("TODO live counts are invalid")
        if self.disposition == "ACTIVE" and not self.ordered_items:
            raise ValueError("ACTIVE TODO projection must contain items")
        if self.disposition in {"CLEARED", "CLOSED"} and (
            self.ordered_items
            or self.pending_count
            or self.in_progress_count
            or self.completed_count
        ):
            raise ValueError("empty TODO disposition carries items")
        canonical_items = todo_snapshot_canonical_json(
            tuple((item.text, item.status) for item in self.ordered_items)
        )
        if len(canonical_items) > MAXIMUM_TODO_CANONICAL_JSON_BYTES:
            raise ValueError("TODO live snapshot exceeds its aggregate bound")


LivePayload: TypeAlias = (
    TextStartPayload
    | TextDeltaPayload
    | TextEndPayload
    | ThinkingStartPayload
    | ThinkingDeltaPayload
    | ThinkingEndPayload
    | DataStartPayload
    | DataDeltaPayload
    | DataEndPayload
    | ToolCallStartPayload
    | ToolCallDeltaPayload
    | ToolCallEndPayload
    | ToolResultStartPayload
    | ToolResultDeltaPayload
    | ToolResultEndPayload
    | InteractionOpenedPayload
    | InteractionReplacedPayload
    | InteractionClosedPayload
    | TerminalProcessCompletedPayload
    | TerminalMonitorOpenedPayload
    | TerminalMonitorObservationPayload
    | TerminalMonitorClosedPayload
    | SubagentProgressPayload
    | TodoSnapshotUpdatedPayload
)


ProviderStreamPayload: TypeAlias = (
    TextStartPayload
    | TextDeltaPayload
    | TextEndPayload
    | ThinkingStartPayload
    | ThinkingDeltaPayload
    | ThinkingEndPayload
    | DataStartPayload
    | DataDeltaPayload
    | DataEndPayload
    | ToolCallStartPayload
    | ToolCallDeltaPayload
    | ToolCallEndPayload
)

_PROVIDER_STREAM_PAYLOAD_TYPES = (
    TextStartPayload,
    TextDeltaPayload,
    TextEndPayload,
    ThinkingStartPayload,
    ThinkingDeltaPayload,
    ThinkingEndPayload,
    DataStartPayload,
    DataDeltaPayload,
    DataEndPayload,
    ToolCallStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
)


def is_provider_stream_payload(value: object) -> TypeGuard[ProviderStreamPayload]:
    return isinstance(value, _PROVIDER_STREAM_PAYLOAD_TYPES)


def payload_to_mapping(payload: LivePayload) -> Mapping[str, object]:
    return asdict(payload)


__all__ = [
    "DataDeltaPayload",
    "DataEndPayload",
    "DataStartPayload",
    "InteractionClosedPayload",
    "InteractionOpenedPayload",
    "InteractionReplacedPayload",
    "LivePayload",
    "ProviderStreamPayload",
    "SubagentProgressPayload",
    "TodoLiveItemProjection",
    "TodoSnapshotUpdatedPayload",
    "TerminalMonitorClosedPayload",
    "TerminalMonitorObservationPayload",
    "TerminalMonitorOpenedPayload",
    "TerminalProcessCompletedPayload",
    "TextDeltaPayload",
    "TextEndPayload",
    "TextStartPayload",
    "ThinkingDeltaPayload",
    "ThinkingEndPayload",
    "ThinkingStartPayload",
    "ToolCallDeltaPayload",
    "ToolCallEndPayload",
    "ToolCallStartPayload",
    "ToolResultDeltaPayload",
    "ToolResultEndPayload",
    "ToolResultStartPayload",
    "live_digest",
    "is_provider_stream_payload",
    "payload_to_mapping",
]
