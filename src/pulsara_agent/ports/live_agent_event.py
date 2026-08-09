"""Renderer-neutral payload vocabulary for the exact 23 live events.

This module is the final type owner shared by provider adapters, the Runtime
live bus, hooks, and Protocol v3.  Keeping these values in ``ports`` prevents
the adapter boundary from inventing a second per-delta vocabulary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Mapping, TypeAlias, TypeGuard


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
