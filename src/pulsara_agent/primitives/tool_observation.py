"""Event-safe, provider-neutral tool observation timing facts."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, field_validator, model_validator

from pulsara_agent.primitives._context_base import (
    FrozenContextFact,
    canonical_utc_timestamp,
)


class ToolObservationTimingFact(FrozenContextFact):
    observed_at_utc: str
    source_started_at_utc: str | None = None
    source_ended_at_utc: str | None = None
    observation_duration_seconds: float | None = Field(default=None, ge=0)
    tool_reported_duration_seconds: float | None = Field(default=None, ge=0)
    freshness: Literal[
        "current_tool_observation",
        "background_process_observation",
        "historical_tool_observation",
        "suspended_tool_observation",
        "unknown",
    ] = "current_tool_observation"
    clock_source: Literal[
        "tool_result_events",
        "tool_runtime_metadata",
        "mixed",
    ] = "tool_result_events"
    tool_origin: Literal[
        "builtin",
        "terminal",
        "mcp",
        "custom",
        "workflow",
        "subagent_system",
        "unknown",
    ] = "unknown"
    tool_name: str | None = None
    tool_call_id: str | None = None
    suspended_at_utc: str | None = None
    resumed_at_utc: str | None = None

    @field_validator(
        "observed_at_utc",
        "source_started_at_utc",
        "source_ended_at_utc",
        "suspended_at_utc",
        "resumed_at_utc",
    )
    @classmethod
    def _utc(cls, value: str | None) -> str | None:
        return canonical_utc_timestamp(value) if value is not None else None

    @field_validator("observation_duration_seconds", "tool_reported_duration_seconds")
    @classmethod
    def _finite_duration(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("duration must be finite")
        return value

    @model_validator(mode="after")
    def _ordered_timestamps(self) -> "ToolObservationTimingFact":
        if (
            self.source_started_at_utc is not None
            and self.source_ended_at_utc is not None
            and self.source_started_at_utc > self.source_ended_at_utc
        ):
            raise ValueError("tool source end precedes source start")
        if (
            self.suspended_at_utc is not None
            and self.resumed_at_utc is not None
            and self.suspended_at_utc > self.resumed_at_utc
        ):
            raise ValueError("tool resume precedes suspension")
        return self

    def to_message_projection_payload(self) -> dict[str, object]:
        """Project the typed fact for message/UI metadata only."""
        values = {
            "observed_at": self.observed_at_utc,
            "source_started_at": self.source_started_at_utc,
            "source_ended_at": self.source_ended_at_utc,
            "observation_duration_seconds": self.observation_duration_seconds,
            "tool_reported_duration_seconds": self.tool_reported_duration_seconds,
            "freshness": self.freshness,
            "clock_source": self.clock_source,
            "tool_origin": self.tool_origin,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "suspended_at": self.suspended_at_utc,
            "resumed_at": self.resumed_at_utc,
        }
        return {key: value for key, value in values.items() if value is not None}


__all__ = ["ToolObservationTimingFact"]
