"""Pure provider-neutral ToolResult projection and delivery contracts.

This module owns the one Round 7 outer envelope and its logical UTF-8 quote.
It performs no I/O and imports no Kernel, repository, compiler, or provider
adapter authority.  Artifact and future bounded page factories use the same
renderer instead of copying its field list or byte formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
from typing import Mapping

from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
)
from pulsara_agent.primitives.tool_observation import (
    FrozenToolObservationTimingFact,
    MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS,
    ToolObservationDurationDisposition,
    ToolObservationOrigin,
    canonical_utc_timestamp,
    tool_observation_timing_fingerprint,
)


TOOL_RESULT_LOGICAL_PROJECTION_CONTRACT = (
    "pulsara.provider-visible-tool-result.logical-projection.v2"
)
TOOL_RESULT_FULL_DELIVERY_CLASSIFIER_CONTRACT = (
    "pulsara.tool-result-full-delivery-classifier.v1"
)
MAXIMUM_TOOL_RESULT_CITATION_HANDLE_UTF8_BYTES = 128
MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_UTF8_BYTES = 8 * 1024
MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_ITEMS = 50


class ToolResultLogicalMessageKind(StrEnum):
    TOOL_RESULT = "TOOL_RESULT"
    LATE_TOOL_OUTCOME = "LATE_TOOL_OUTCOME"


class ToolResultDeliveryRequirement(StrEnum):
    BEST_AVAILABLE = "BEST_AVAILABLE"
    FULL_REQUIRED = "FULL_REQUIRED"


class ToolResultFullDeliveryReason(StrEnum):
    ARTIFACT_PAGE = "ARTIFACT_PAGE"
    MCP_DIRECTORY_PAGE = "MCP_DIRECTORY_PAGE"
    MCP_INSPECT_SCHEMA = "MCP_INSPECT_SCHEMA"
    SKILL_ACTIVATION = "SKILL_ACTIVATION"


@dataclass(frozen=True, slots=True)
class FrozenToolResultDeliveryRequirement:
    requirement: ToolResultDeliveryRequirement
    reason: ToolResultFullDeliveryReason | None
    classifier_contract: str = TOOL_RESULT_FULL_DELIVERY_CLASSIFIER_CONTRACT

    def __post_init__(self) -> None:
        if not isinstance(self.requirement, ToolResultDeliveryRequirement):
            raise TypeError("tool result delivery requirement is invalid")
        if self.reason is not None and not isinstance(
            self.reason, ToolResultFullDeliveryReason
        ):
            raise TypeError("tool result full-delivery reason is invalid")
        if self.classifier_contract != TOOL_RESULT_FULL_DELIVERY_CLASSIFIER_CONTRACT:
            raise ValueError("tool result delivery classifier contract drifted")
        if (
            self.requirement is ToolResultDeliveryRequirement.FULL_REQUIRED
        ) != (self.reason is not None):
            raise ValueError("tool result delivery requirement union is invalid")


BEST_AVAILABLE_TOOL_RESULT_DELIVERY = FrozenToolResultDeliveryRequirement(
    ToolResultDeliveryRequirement.BEST_AVAILABLE,
    None,
)


def full_required_tool_result_delivery(
    reason: ToolResultFullDeliveryReason,
) -> FrozenToolResultDeliveryRequirement:
    return FrozenToolResultDeliveryRequirement(
        ToolResultDeliveryRequirement.FULL_REQUIRED,
        reason,
    )


def classify_tool_result_delivery(
    *,
    tool_name: str,
    arguments: FrozenJsonObjectFact,
    result_state: str,
) -> FrozenToolResultDeliveryRequirement:
    """Rebuild the closed requirement from exact canonical request/result facts.

    Round 7.1 activates only the existing ``artifact_read`` text page.  The
    remaining reason values are frozen for later rounds and can be exercised
    synthetically without advertising or implementing those capabilities now.
    """

    if result_state != "SUCCESS" or tool_name != "artifact_read":
        return BEST_AVAILABLE_TOOL_RESULT_DELIVERY
    values = {entry.key: entry.value for entry in arguments.entries}
    mode = values.get("mode", "text")
    if mode == "text":
        return full_required_tool_result_delivery(
            ToolResultFullDeliveryReason.ARTIFACT_PAGE
        )
    return BEST_AVAILABLE_TOOL_RESULT_DELIVERY


@dataclass(frozen=True, slots=True)
class RenderedProviderToolResultLogicalMessage:
    message: LLMMessage
    logical_utf8_bytes: int
    content: str

    def __post_init__(self) -> None:
        if self.logical_utf8_bytes != provider_neutral_message_logical_utf8_bytes(
            self.message
        ):
            raise ValueError("tool result logical message quote mismatch")
        if self.message.content != (self.content,):
            raise ValueError("tool result logical content differs from its message")


def render_provider_tool_result_logical_message(
    *,
    message_kind: ToolResultLogicalMessageKind,
    tool_call_id: str,
    body: str,
    result_state: str,
    timing: FrozenToolObservationTimingFact,
    citation_handle: str | None,
    model_visible_memory_ids: tuple[str, ...],
) -> RenderedProviderToolResultLogicalMessage:
    """Render the exact Round 7 outer carrier and quote its actual scalars."""

    if not tool_call_id or not result_state:
        raise ValueError("tool result logical identity is incomplete")
    body.encode("utf-8")
    if citation_handle is not None and (
        not citation_handle.startswith("tool:")
        or len(citation_handle.encode("utf-8"))
        > MAXIMUM_TOOL_RESULT_CITATION_HANDLE_UTF8_BYTES
    ):
        raise ValueError("tool result citation handle is invalid")
    _validate_model_visible_memory_ids(model_visible_memory_ids)

    payload = {
        "pulsara_tool_result": {
            "body": body,
            "citation_handle": citation_handle,
            "model_visible_memory_ids": list(model_visible_memory_ids),
            "observation": {
                "duration_disposition": timing.duration_disposition.value,
                "observation_duration_microseconds": (
                    timing.observation_duration_microseconds
                ),
                "observation_origin": timing.observation_origin.value,
                "observed_at_utc": timing.observed_at_utc,
                "source_turn_ref": timing.source_turn_ref,
                "tool_reported_duration_microseconds": (
                    timing.tool_reported_duration_microseconds
                ),
            },
            "result_state": result_state,
        }
    }
    content = canonical_json_bytes(payload).decode("utf-8")
    decode_provider_tool_result_observation(content)
    if message_kind is ToolResultLogicalMessageKind.TOOL_RESULT:
        message = LLMMessage.tool_result(content, tool_call_id=tool_call_id)
    elif message_kind is ToolResultLogicalMessageKind.LATE_TOOL_OUTCOME:
        message = LLMMessage.user(
            canonical_json_bytes(
                {
                    "pulsara_late_tool_outcome": {
                        "result": payload["pulsara_tool_result"],
                        "tool_call_id": tool_call_id,
                    }
                }
            ).decode("utf-8")
        )
        content = message.content[0]
    else:  # pragma: no cover - StrEnum closes ordinary construction.
        raise TypeError(message_kind)
    return RenderedProviderToolResultLogicalMessage(
        message=message,
        logical_utf8_bytes=provider_neutral_message_logical_utf8_bytes(message),
        content=content,
    )


def provider_neutral_message_logical_utf8_bytes(message: LLMMessage) -> int:
    """Count only provider-neutral string scalars, never adapter JSON framing."""

    values = [*message.content, *message.thinking]
    for call in message.tool_calls:
        values.extend((call.id, call.name, call.arguments))
    values.extend(
        value
        for value in (message.tool_call_id, message.name, message.arguments)
        if value is not None
    )
    return sum(len(value.encode("utf-8")) for value in values)


def conservative_artifact_page_logical_utf8_bytes(
    *,
    tool_call_id: str,
    body: str,
    model_visible_memory_ids: tuple[str, ...],
) -> int:
    """Quote an artifact page under the largest legal call-local augmentation.

    The exact call ID and memory provenance are already frozen by the tool
    invocation.  Timing and citation values use their closed maxima.  Taking
    the maximum of ordinary and late carriers keeps a cancelled/late exact
    page representable without changing its canonicalized body.
    """

    timing = _maximum_artifact_page_timing()
    citation = "tool:" + ("x" * (MAXIMUM_TOOL_RESULT_CITATION_HANDLE_UTF8_BYTES - 5))
    quotes = tuple(
        render_provider_tool_result_logical_message(
            message_kind=kind,
            tool_call_id=tool_call_id,
            body=body,
            result_state="SUCCESS",
            timing=timing,
            citation_handle=citation,
            model_visible_memory_ids=model_visible_memory_ids,
        ).logical_utf8_bytes
        for kind in ToolResultLogicalMessageKind
    )
    return max(quotes)


def decode_provider_tool_result_observation(text: str) -> Mapping[str, object]:
    """Decode and fixed-point validate one provider-visible result envelope."""

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("tool result observation is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"pulsara_tool_result"}:
        raise ValueError("tool result observation outer contract is invalid")
    payload = value["pulsara_tool_result"]
    if not isinstance(payload, dict) or set(payload) != {
        "body",
        "citation_handle",
        "model_visible_memory_ids",
        "observation",
        "result_state",
    }:
        raise ValueError("tool result observation member contract is invalid")
    if not isinstance(payload["body"], str) or not isinstance(
        payload["result_state"], str
    ):
        raise ValueError("tool result observation scalar contract is invalid")
    citation_handle = payload["citation_handle"]
    if citation_handle is not None and (
        not isinstance(citation_handle, str)
        or not citation_handle.startswith("tool:")
        or len(citation_handle.encode("utf-8"))
        > MAXIMUM_TOOL_RESULT_CITATION_HANDLE_UTF8_BYTES
    ):
        raise ValueError("tool result citation handle is invalid")
    memory_ids = payload["model_visible_memory_ids"]
    if not isinstance(memory_ids, list):
        raise ValueError("tool result memory provenance header is invalid")
    _validate_model_visible_memory_ids(tuple(memory_ids))
    observation = payload["observation"]
    if not isinstance(observation, dict) or set(observation) != {
        "duration_disposition",
        "observation_duration_microseconds",
        "observation_origin",
        "observed_at_utc",
        "source_turn_ref",
        "tool_reported_duration_microseconds",
    }:
        raise ValueError("tool timing observation contract is invalid")
    source_turn_ref = observation["source_turn_ref"]
    if (
        not isinstance(source_turn_ref, str)
        or len(source_turn_ref) != 71
        or not source_turn_ref.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in source_turn_ref[7:])
    ):
        raise ValueError("tool timing turn reference is invalid")
    observed_at = observation["observed_at_utc"]
    if not isinstance(observed_at, str):
        raise ValueError("tool timing timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("tool timing timestamp is invalid") from exc
    if canonical_utc_timestamp(parsed) != observed_at:
        raise ValueError("tool timing timestamp is not canonical UTC")
    disposition = ToolObservationDurationDisposition(
        observation["duration_disposition"]
    )
    origin = ToolObservationOrigin(observation["observation_origin"])
    duration = observation["observation_duration_microseconds"]
    reported = observation["tool_reported_duration_microseconds"]
    if duration is not None and (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or not 0 <= duration <= MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS
    ):
        raise ValueError("tool timing duration is invalid")
    if reported is not None and (
        not isinstance(reported, int)
        or isinstance(reported, bool)
        or not 0 <= reported <= MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS
    ):
        raise ValueError("tool-reported duration is invalid")
    if (disposition is ToolObservationDurationDisposition.MEASURED) != (
        duration is not None
    ):
        raise ValueError("tool timing disposition is inconsistent")
    nonphysical = origin in {
        ToolObservationOrigin.POLICY,
        ToolObservationOrigin.PLAN_CONTROL,
    }
    if nonphysical != (
        disposition is ToolObservationDurationDisposition.NO_PHYSICAL_ATTEMPT
    ):
        raise ValueError("tool timing origin is inconsistent")
    if nonphysical and reported is not None:
        raise ValueError("nonphysical tool timing reports a duration")
    if canonical_json_bytes(value).decode("utf-8") != text:
        raise ValueError("tool result observation is not canonical JSON")
    return payload


def _maximum_artifact_page_timing() -> FrozenToolObservationTimingFact:
    values = {
        "source_turn_ref": "sha256:" + ("f" * 64),
        "observed_at_utc": "9999-12-31T23:59:59.999999Z",
        "observation_duration_microseconds": (
            MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS
        ),
        "duration_disposition": ToolObservationDurationDisposition.MEASURED,
        "tool_reported_duration_microseconds": (
            MAXIMUM_TOOL_OBSERVATION_DURATION_MICROSECONDS
        ),
        "observation_origin": ToolObservationOrigin.BUILTIN,
    }
    provisional = FrozenToolObservationTimingFact.__new__(
        FrozenToolObservationTimingFact
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return FrozenToolObservationTimingFact(
        **values,
        fact_fingerprint=tool_observation_timing_fingerprint(provisional),
    )


def _validate_model_visible_memory_ids(values: tuple[str, ...]) -> None:
    if (
        len(values) > MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_ITEMS
        or any(not isinstance(value, str) or not value for value in values)
        or len(set(values)) != len(values)
        or len(canonical_json_bytes(values))
        > MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_UTF8_BYTES
    ):
        raise ValueError("tool result memory provenance header is invalid")


__all__ = [
    "BEST_AVAILABLE_TOOL_RESULT_DELIVERY",
    "FrozenToolResultDeliveryRequirement",
    "MAXIMUM_TOOL_RESULT_CITATION_HANDLE_UTF8_BYTES",
    "MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_ITEMS",
    "MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_UTF8_BYTES",
    "RenderedProviderToolResultLogicalMessage",
    "TOOL_RESULT_FULL_DELIVERY_CLASSIFIER_CONTRACT",
    "TOOL_RESULT_LOGICAL_PROJECTION_CONTRACT",
    "ToolResultDeliveryRequirement",
    "ToolResultFullDeliveryReason",
    "ToolResultLogicalMessageKind",
    "classify_tool_result_delivery",
    "conservative_artifact_page_logical_utf8_bytes",
    "decode_provider_tool_result_observation",
    "full_required_tool_result_delivery",
    "provider_neutral_message_logical_utf8_bytes",
    "render_provider_tool_result_logical_message",
]
