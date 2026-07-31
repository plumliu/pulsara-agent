"""Process-local inputs for durable ToolResult semantics builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.primitives.context import (
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.tool_result import (
    TerminalPayloadTimingFact,
    ToolResultDomainSubmissionFact,
    ToolResultErrorPreviewFact,
    ToolResultRenderVariantCode,
)


class ToolResultSemanticsRuntimeInput(Protocol):
    semantics_input_kind: ToolResultRenderVariantCode

    def to_frozen_domain_submission(self) -> ToolResultDomainSubmissionFact | None: ...


@dataclass(frozen=True, slots=True)
class FrozenToolResultSemanticsRuntimeInput:
    semantics_input_kind: ToolResultRenderVariantCode
    domain_submission: ToolResultDomainSubmissionFact | None

    def to_frozen_domain_submission(self) -> ToolResultDomainSubmissionFact | None:
        return self.domain_submission


def unbounded_error_preview(value: str) -> ToolResultErrorPreviewFact:
    return ToolResultErrorPreviewFact(
        text=value,
        original_chars=len(value),
        truncated=False,
    )


def build_terminal_payload_timing(
    *,
    observed_at_utc: str,
    duration_seconds: float | None,
    freshness: str,
    clock_source: str,
    command_started_at_utc: str | None = None,
    process_started_at_utc: str | None = None,
    last_output_at_utc: str | None = None,
) -> TerminalPayloadTimingFact:
    timing_payload = {
        "observed_at_utc": canonical_utc_timestamp(observed_at_utc),
        "duration_seconds": (
            float(duration_seconds) if duration_seconds is not None else None
        ),
        "freshness": freshness,
        "clock_source": clock_source,
        "command_started_at_utc": (
            canonical_utc_timestamp(command_started_at_utc)
            if command_started_at_utc is not None
            else None
        ),
        "process_started_at_utc": (
            canonical_utc_timestamp(process_started_at_utc)
            if process_started_at_utc is not None
            else None
        ),
        "last_output_at_utc": (
            canonical_utc_timestamp(last_output_at_utc)
            if last_output_at_utc is not None
            else None
        ),
    }
    return TerminalPayloadTimingFact(
        **timing_payload,
        timing_fingerprint=context_fingerprint(
            "terminal-payload-timing:v1", timing_payload
        ),
    )


__all__ = [
    "FrozenToolResultSemanticsRuntimeInput",
    "ToolResultSemanticsRuntimeInput",
    "build_terminal_payload_timing",
    "unbounded_error_preview",
]
