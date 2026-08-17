"""Closed immutable facts for model-visible tool observations.

The values here are deliberately independent from repositories, transports and
tool payloads.  A physical invocation owner may create the process-local
supplements, while only canonical rows can create the frozen provider fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from pulsara_agent.primitives.context import context_fingerprint


MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS = 31_536_000_000_000
MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES = 40_000


class ToolObservationDurationDisposition(StrEnum):
    MEASURED = "MEASURED"
    NO_PHYSICAL_ATTEMPT = "NO_PHYSICAL_ATTEMPT"
    MEASUREMENT_UNAVAILABLE = "MEASUREMENT_UNAVAILABLE"


class ToolObservationOrigin(StrEnum):
    BUILTIN = "BUILTIN"
    TERMINAL_PROCESS = "TERMINAL_PROCESS"
    MCP_REMOTE = "MCP_REMOTE"
    POLICY = "POLICY"
    PLAN_CONTROL = "PLAN_CONTROL"
    CUSTOM_OR_UNKNOWN = "CUSTOM_OR_UNKNOWN"


def canonical_utc_timestamp(value: datetime) -> str:
    """Return one canonical UTC timestamp suitable for JSON and fingerprints."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("tool observation timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def normalize_observation_duration(value: int | None) -> int | None:
    """Drop only an invalid measurement; a known result remains authoritative."""

    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if not 0 <= value <= MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS:
        return None
    return value


@dataclass(frozen=True, slots=True)
class PhysicalToolObservationSupplement:
    """Process-local outcome time frozen at the exact physical boundary."""

    observed_at: datetime
    elapsed_microseconds: int | None
    observation_origin_kind: ToolObservationOrigin

    def __post_init__(self) -> None:
        canonical_utc_timestamp(self.observed_at)
        if self.observation_origin_kind in {
            ToolObservationOrigin.POLICY,
            ToolObservationOrigin.PLAN_CONTROL,
        }:
            raise ValueError("physical observation cannot use a nonphysical origin")
        if self.elapsed_microseconds != normalize_observation_duration(
            self.elapsed_microseconds
        ):
            raise ValueError("physical observation duration is invalid")


@dataclass(frozen=True, slots=True)
class TrustedToolObservationSupplement:
    """Optional sealed first-party duration; never inferred from tool JSON."""

    duration_microseconds: int | None

    def __post_init__(self) -> None:
        if self.duration_microseconds != normalize_observation_duration(
            self.duration_microseconds
        ):
            raise ValueError("trusted tool-reported duration is invalid")


@dataclass(frozen=True, slots=True)
class FrozenToolObservationTimingFact:
    source_turn_ref: str
    observed_at_utc: str
    observation_duration_microseconds: int | None
    duration_disposition: ToolObservationDurationDisposition
    tool_reported_duration_microseconds: int | None
    observation_origin: ToolObservationOrigin
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if (
            len(self.source_turn_ref) != 71
            or not self.source_turn_ref.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in self.source_turn_ref[7:]
            )
        ):
            raise ValueError("tool observation turn reference is invalid")
        parsed = datetime.fromisoformat(self.observed_at_utc.replace("Z", "+00:00"))
        if canonical_utc_timestamp(parsed) != self.observed_at_utc:
            raise ValueError("tool observation timestamp is not canonical UTC")
        measured = self.duration_disposition is (
            ToolObservationDurationDisposition.MEASURED
        )
        if measured != (self.observation_duration_microseconds is not None):
            raise ValueError("tool observation duration disposition is inconsistent")
        if self.observation_duration_microseconds != normalize_observation_duration(
            self.observation_duration_microseconds
        ):
            raise ValueError("tool observation duration is invalid")
        if self.tool_reported_duration_microseconds != normalize_observation_duration(
            self.tool_reported_duration_microseconds
        ):
            raise ValueError("tool-reported duration is invalid")
        nonphysical = self.observation_origin in {
            ToolObservationOrigin.POLICY,
            ToolObservationOrigin.PLAN_CONTROL,
        }
        if nonphysical != (
            self.duration_disposition
            is ToolObservationDurationDisposition.NO_PHYSICAL_ATTEMPT
        ):
            raise ValueError("tool observation origin/disposition is inconsistent")
        if nonphysical and self.tool_reported_duration_microseconds is not None:
            raise ValueError("nonphysical observation cannot report a duration")
        if self.fact_fingerprint != tool_observation_timing_fingerprint(self):
            raise ValueError("tool observation timing fingerprint mismatch")


def tool_observation_timing_fingerprint(
    fact: FrozenToolObservationTimingFact,
) -> str:
    return context_fingerprint(
        "pulsara:tool-observation-timing:v1",
        {
            "source_turn_ref": fact.source_turn_ref,
            "observed_at_utc": fact.observed_at_utc,
            "observation_duration_microseconds": (
                fact.observation_duration_microseconds
            ),
            "duration_disposition": fact.duration_disposition.value,
            "tool_reported_duration_microseconds": (
                fact.tool_reported_duration_microseconds
            ),
            "observation_origin": fact.observation_origin.value,
        },
    )


def provider_visible_turn_ref(*, session_id: str, turn_id: str) -> str:
    if not session_id or not turn_id:
        raise ValueError("provider-visible turn reference identity is incomplete")
    return context_fingerprint(
        "pulsara:provider-visible-turn-ref:v1",
        {"session_id": session_id, "turn_id": turn_id},
    )


__all__ = [
    "FrozenToolObservationTimingFact",
    "MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS",
    "MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES",
    "PhysicalToolObservationSupplement",
    "ToolObservationDurationDisposition",
    "ToolObservationOrigin",
    "TrustedToolObservationSupplement",
    "canonical_utc_timestamp",
    "normalize_observation_duration",
    "provider_visible_turn_ref",
    "tool_observation_timing_fingerprint",
]
