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

from pulsara_agent.llm.input import (
    LLMMessage,
    LLMTextPart,
    llm_content_logical_bytes,
    text_part_values,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import (
    FrozenToolObservationTimingFact,
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
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
    result_state: str,
) -> FrozenToolResultDeliveryRequirement:
    """Rebuild the closed requirement from exact tool identity and result state.

    MCP directory pages and inspection schemas are meaningful only when their
    exact closed payload reaches the model in FULL.
    """

    if result_state != "SUCCESS":
        return BEST_AVAILABLE_TOOL_RESULT_DELIVERY
    if tool_name in {
        "list_mcp_prompts",
        "list_mcp_resource_templates",
        "list_mcp_resources",
        "list_mcp_servers",
    }:
        return full_required_tool_result_delivery(
            ToolResultFullDeliveryReason.MCP_DIRECTORY_PAGE
        )
    if tool_name == "inspect_new_mcp_tool":
        return full_required_tool_result_delivery(
            ToolResultFullDeliveryReason.MCP_INSPECT_SCHEMA
        )
    if tool_name != "artifact_read":
        return BEST_AVAILABLE_TOOL_RESULT_DELIVERY
    return full_required_tool_result_delivery(
        ToolResultFullDeliveryReason.ARTIFACT_PAGE
    )


def project_tool_result_storage_body(
    body: str, observation_origin: ToolObservationOrigin
) -> str:
    """Project storage-only carriers into the single Round 7 public body."""

    if observation_origin is not ToolObservationOrigin.PLAN_CONTROL:
        return body
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return canonical_json_bytes(
            {"plan_control": "REJECTED", "status": "error"}
        ).decode("utf-8")
    if not isinstance(value, dict):
        raise ValueError("Plan result carrier is not an object")
    status = value.get("status")
    control = value.get("plan_control")
    if status != "success" or control not in {
        "ENTERED_PLAN",
        "PLAN_ALREADY_ACTIVE",
        "DRAFT_SUBMITTED_FOR_REVIEW",
        "QUESTION_ANSWERED",
    }:
        raise ValueError("Plan control result storage carrier is invalid")
    result: dict[str, object] = {"plan_control": control, "status": status}
    if control == "QUESTION_ANSWERED":
        answer_kind = value.get("answer_kind")
        if answer_kind == "OPTION":
            ordinal = value.get("selected_option_ordinal")
            label = value.get("selected_label")
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal < 0
                or not isinstance(label, str)
            ):
                raise ValueError("Plan option answer storage carrier is invalid")
            result["answer"] = {
                "kind": "OPTION",
                "label": label,
                "ordinal": ordinal,
            }
        elif answer_kind == "FREE_TEXT" and isinstance(value.get("answer"), str):
            result["answer"] = {
                "kind": "FREE_TEXT",
                "text": value["answer"],
            }
        else:
            raise ValueError("Plan question answer storage carrier is invalid")
    return canonical_json_bytes(result).decode("utf-8")


@dataclass(frozen=True, slots=True)
class RenderedProviderToolResultLogicalMessage:
    message: LLMMessage
    logical_utf8_bytes: int
    content: str

    def __post_init__(self) -> None:
        if self.logical_utf8_bytes != provider_neutral_message_logical_bytes(
            self.message
        ):
            raise ValueError("tool result logical message quote mismatch")
        if self.message.content != (LLMTextPart(self.content),):
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
        content = text_part_values(message.content)[0]
    else:  # pragma: no cover - StrEnum closes ordinary construction.
        raise TypeError(message_kind)
    return RenderedProviderToolResultLogicalMessage(
        message=message,
        logical_utf8_bytes=provider_neutral_message_logical_bytes(message),
        content=content,
    )


def provider_neutral_message_logical_bytes(message: LLMMessage) -> int:
    """Count provider-neutral scalars/image bytes, never adapter JSON framing."""

    content_bytes = llm_content_logical_bytes(message.content)
    values = [*message.thinking]
    for call in message.tool_calls:
        values.extend((call.id, call.name, call.arguments))
    values.extend(
        value
        for value in (message.tool_call_id, message.name, message.arguments)
        if value is not None
    )
    return content_bytes + sum(len(value.encode("utf-8")) for value in values)


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


def conservative_tool_result_logical_message(
    *,
    message_kind: ToolResultLogicalMessageKind,
    tool_call_id: str,
) -> RenderedProviderToolResultLogicalMessage:
    """Build the largest existing FULL projection needed before Tool execution.

    The normal compiler may select FULL whenever the complete rendered message
    fits ``MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES``.  Before an effect
    runs the result body, timing, citation and memory provenance are unknown.
    Their largest scalar values do not maximize the eventual provider wire:
    they consume the same logical budget with mostly unescaped text.  Use the
    smallest legal fixed envelope and spend every remaining byte on a body of
    JSON-escaping characters instead.  This bounds both wire bytes and the D1
    JSON-character estimate for every legal FULL projection with this exact
    call ID.  It is a process-local admission value; it is never sent, stored,
    or offered as a ToolResult variant.
    """

    timing = _minimum_full_projection_timing()

    def render(body_chars: int) -> RenderedProviderToolResultLogicalMessage:
        return render_provider_tool_result_logical_message(
            message_kind=message_kind,
            tool_call_id=tool_call_id,
            body="\\" * body_chars,
            result_state="SUCCESS",
            timing=timing,
            citation_handle=None,
            model_visible_memory_ids=(),
        )

    empty = render(0)
    if empty.logical_utf8_bytes >= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
        return empty
    low = 0
    high = MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    winner = empty
    while low <= high:
        middle = (low + high) // 2
        candidate = render(middle)
        if (
            candidate.logical_utf8_bytes
            <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
        ):
            winner = candidate
            low = middle + 1
        else:
            high = middle - 1
    remaining = MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES - (
        winner.logical_utf8_bytes
    )
    if remaining:
        candidate = render_provider_tool_result_logical_message(
            message_kind=message_kind,
            tool_call_id=tool_call_id,
            body=("\\" * high) + ("x" * remaining),
            result_state="SUCCESS",
            timing=timing,
            citation_handle=None,
            model_visible_memory_ids=(),
        )
        if (
            candidate.logical_utf8_bytes
            <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
        ):
            winner = candidate
    return winner


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


def _minimum_full_projection_timing() -> FrozenToolObservationTimingFact:
    """Return the shortest legal timing envelope for the FULL wire upper."""

    values = {
        "source_turn_ref": "sha256:" + ("0" * 64),
        "observed_at_utc": "1970-01-01T00:00:00.000000Z",
        "observation_duration_microseconds": 0,
        "duration_disposition": ToolObservationDurationDisposition.MEASURED,
        "tool_reported_duration_microseconds": 0,
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
    "conservative_tool_result_logical_message",
    "decode_provider_tool_result_observation",
    "full_required_tool_result_delivery",
    "provider_neutral_message_logical_bytes",
    "project_tool_result_storage_body",
    "render_provider_tool_result_logical_message",
]
