"""Canonical best-effort storage for provider-reported per-call usage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


_CALL_ID = re.compile(r"^model_call:[0-9a-f]{32}$")
_MAX_BIGINT = 2**63 - 1
_USAGE_STATUSES = frozenset({"reported", "partial", "missing", "invalid"})
_WIRE_APIS = frozenset({"openai_chat_completions", "openai_responses"})
_TERMINAL_KINDS = frozenset({"COMPLETED", "OUTPUT_INCOMPLETE", "PROVIDER_ERROR"})


class ProviderCallUsageWriteDisposition(StrEnum):
    INSERTED = "INSERTED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class ProviderCallUsageObservation:
    """Frozen observation payload for one opened AGENT_MODEL_LOOP execution."""

    session_id: str
    turn_id: str
    resolved_model_call_id: str
    model_call_index: int
    connection_id: str
    route_id: str
    wire_api: str
    requested_model_id: str
    reported_model_id: str | None
    normalized_terminal_kind: str | None
    usage_status: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    reported_total_tokens: int | None = None
    diagnostic_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "turn_id",
            "connection_id",
            "route_id",
            "requested_model_id",
        ):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed string")
        if _CALL_ID.fullmatch(self.resolved_model_call_id) is None:
            raise ValueError("resolved_model_call_id is invalid")
        if type(self.model_call_index) is not int or self.model_call_index < 0:
            raise ValueError("model_call_index must be a non-negative integer")
        if self.wire_api not in _WIRE_APIS:
            raise ValueError("wire_api is invalid")
        if self.reported_model_id is not None and (
            not self.reported_model_id
            or self.reported_model_id != self.reported_model_id.strip()
        ):
            raise ValueError("reported_model_id must be null or a trimmed string")
        if self.normalized_terminal_kind is not None and (
            self.normalized_terminal_kind not in _TERMINAL_KINDS
        ):
            raise ValueError("normalized_terminal_kind is invalid")
        if self.usage_status not in _USAGE_STATUSES:
            raise ValueError("usage_status is invalid")
        for name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
            "reported_total_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not int or not 0 <= value <= _MAX_BIGINT
            ):
                raise ValueError(f"{name} must be null or a PostgreSQL bigint")
        if (
            self.cached_input_tokens is not None
            and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached_input_tokens exceeds input_tokens")
        if (
            self.reasoning_output_tokens is not None
            and self.output_tokens is not None
            and self.reasoning_output_tokens > self.output_tokens
        ):
            raise ValueError("reasoning_output_tokens exceeds output_tokens")
        if not isinstance(self.diagnostic_codes, tuple) or any(
            not isinstance(code, str) or not code or len(code) > 96
            for code in self.diagnostic_codes
        ):
            raise ValueError("diagnostic_codes must be a tuple of typed codes")
        has_any_count = any(
            getattr(self, name) is not None
            for name in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "reasoning_output_tokens",
                "reported_total_tokens",
            )
        )
        has_input_output_pair = (
            self.input_tokens is not None and self.output_tokens is not None
        )
        if self.usage_status == "reported" and not has_input_output_pair:
            raise ValueError("reported usage requires input and output counts")
        if self.usage_status == "partial" and (
            not has_any_count or has_input_output_pair
        ):
            raise ValueError("partial usage requires an incomplete valid observation")
        if self.usage_status in {"missing", "invalid"} and has_any_count:
            raise ValueError(f"{self.usage_status} usage cannot contain valid counts")
        if self.usage_status == "invalid" and not self.diagnostic_codes:
            raise ValueError("invalid usage requires a diagnostic code")


__all__ = [
    "ProviderCallUsageObservation",
    "ProviderCallUsageWriteDisposition",
]
