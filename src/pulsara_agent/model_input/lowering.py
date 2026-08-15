"""Provider-neutral lowering for canonical conversation facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Mapping

from pulsara_agent.llm.input import LLMMessage, LLMToolCall
from pulsara_agent.model_input.contracts import (
    ContextChannel,
    ContextSourceCandidate,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    StructuredModelInputLimits,
    ToolResultProviderRenderMode,
)
from pulsara_agent.model_input.continuity import (
    SourceObservationLifecycle,
    SourceObservationPresence,
    encode_runtime_observation,
)
from pulsara_agent.ports.artifact import ToolOutputArtifactDisposition
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import (
    ToolObservationDurationDisposition,
    ToolObservationOrigin,
    canonical_utc_timestamp,
    normalize_observation_duration,
)


@dataclass(frozen=True, slots=True)
class LoweredToolResultVariant:
    mode: ToolResultProviderRenderMode
    message: LLMMessage
    utf8_bytes: int


@dataclass(frozen=True, slots=True)
class LoweredCanonicalItem:
    source: FrozenProviderInputItem
    fixed_message: LLMMessage | None
    tool_result_variants: tuple[LoweredToolResultVariant, ...] = ()


def lower_canonical_item(
    item: FrozenProviderInputItem,
    *,
    artifact_read_available: bool,
    limits: StructuredModelInputLimits,
    memory_citation_handles: Mapping[str, str] | None = None,
) -> LoweredCanonicalItem:
    kind = item.item_kind
    if kind in {
        FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
        FrozenProviderInputItemKind.USER,
    }:
        prefix = (
            "[CONTEXT_SNAPSHOT]\n"
            if kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
            else ""
        )
        return LoweredCanonicalItem(item, LLMMessage.user(prefix + item.text))
    if kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION:
        return LoweredCanonicalItem(
            item,
            LLMMessage.user(
                canonical_json_bytes(
                    {
                        "pulsara_terminal_observation": (
                            _project_terminal_observation(item.text)
                        )
                    }
                ).decode("utf-8")
            ),
        )
    if kind is FrozenProviderInputItemKind.PLAN_CONTINUATION:
        return LoweredCanonicalItem(
            item,
            LLMMessage.user(_project_plan_continuation(item.text)),
        )
    if kind is FrozenProviderInputItemKind.ASSISTANT:
        return LoweredCanonicalItem(item, LLMMessage.assistant(item.text))
    if kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
        return LoweredCanonicalItem(
            item,
            LLMMessage.assistant_turn(
                text=item.text or None,
                tool_calls=tuple(
                    LLMToolCall(
                        id=call.tool_call_id,
                        name=call.tool_name,
                        arguments=canonical_json_bytes(call.arguments).decode("utf-8"),
                    )
                    for call in item.tool_calls
                ),
            ),
        )
    if kind is FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE:
        if item.tool_call_id is None:
            raise ValueError("tool result closure lacks call identity")
        return LoweredCanonicalItem(
            item,
            LLMMessage.tool_result(
                _project_tool_result_closure(item.text),
                tool_call_id=item.tool_call_id,
            ),
        )
    if kind in {
        FrozenProviderInputItemKind.TOOL_RESULT,
        FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
    }:
        return LoweredCanonicalItem(
            item,
            None,
            _tool_result_variants(
                item,
                artifact_read_available=artifact_read_available,
                limits=limits,
                citation_handle=(memory_citation_handles or {}).get(
                    item.tool_result_context.result_id
                    if item.tool_result_context is not None
                    else ""
                ),
            ),
        )
    raise TypeError(kind)


def source_variant_message(candidate: ContextSourceCandidate, text: str) -> LLMMessage:
    if candidate.channel is ContextChannel.SYSTEM:
        raise ValueError("SYSTEM source is not an ordered message")
    lifecycle = {
        "SNAPSHOT_ON_CHANGE": SourceObservationLifecycle.SNAPSHOT,
        "CALL_APPEND": SourceObservationLifecycle.CALL,
        "TURN_APPEND": SourceObservationLifecycle.TURN,
        "TURN_SNAPSHOT": SourceObservationLifecycle.TURN,
        "ACTIVATION_SNAPSHOT": SourceObservationLifecycle.ACTIVATION,
        "ONE_SHOT": SourceObservationLifecycle.ONE_SHOT,
    }.get(candidate.lifecycle.value)
    if lifecycle is None:
        raise ValueError("source lifecycle cannot be lowered as an observation")
    return encode_runtime_observation(
        source_kind=candidate.source_kind,
        trust_class=candidate.trust_class,
        lifecycle=lifecycle,
        presence=SourceObservationPresence.VALUE,
        contract_version=candidate.source_contract_version,
        body=text,
    )


def _tool_result_variants(
    item: FrozenProviderInputItem,
    *,
    artifact_read_available: bool,
    limits: StructuredModelInputLimits,
    citation_handle: str | None,
) -> tuple[LoweredToolResultVariant, ...]:
    metadata = item.tool_result_context
    body = item.tool_result_body_text
    if metadata is None or body is None or item.tool_call_id is None:
        raise ValueError("tool result lowering lacks typed metadata")
    result: list[LoweredToolResultVariant] = []

    def append(
        mode: ToolResultProviderRenderMode,
        rendered_body: str,
        *,
        maximum_message_bytes: int | None = None,
    ) -> bool:
        message = _tool_result_message(
            item, rendered_body, citation_handle=citation_handle
        )
        logical_bytes = _message_logical_utf8_bytes(message)
        if maximum_message_bytes is not None and logical_bytes > maximum_message_bytes:
            return False
        result.append(
            LoweredToolResultVariant(
                mode=mode,
                message=message,
                utf8_bytes=logical_bytes,
            )
        )
        return True

    append(ToolResultProviderRenderMode.FULL, body)
    compact = _bounded_compact_tool_result_body(
        item,
        maximum_message_bytes=limits.maximum_tool_result_compact_bytes,
        artifact_read_available=artifact_read_available,
        citation_handle=citation_handle,
    )
    if compact is not None and compact != body:
        append(
            ToolResultProviderRenderMode.COMPACT,
            compact,
            maximum_message_bytes=limits.maximum_tool_result_compact_bytes,
        )
    if (
        artifact_read_available
        and metadata.artifact_id is not None
        and metadata.artifact_disposition
        in {
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolOutputArtifactDisposition.INCOMPLETE,
        }
    ):
        ref = _artifact_reference_body(item)
        append(
            ToolResultProviderRenderMode.REF_ONLY,
            ref,
            maximum_message_bytes=limits.maximum_tool_result_ref_only_bytes,
        )
    append(ToolResultProviderRenderMode.OMITTED_BODY, _omitted_tool_result_body(item))
    return tuple(result)


def _message_logical_utf8_bytes(message: LLMMessage) -> int:
    values = [*message.content, *message.thinking]
    for call in message.tool_calls:
        values.extend((call.id, call.name, call.arguments))
    values.extend(
        value
        for value in (message.tool_call_id, message.name, message.arguments)
        if value is not None
    )
    return sum(len(value.encode("utf-8")) for value in values)


def _tool_result_message(
    item: FrozenProviderInputItem,
    body: str,
    *,
    citation_handle: str | None,
) -> LLMMessage:
    assert item.tool_call_id is not None
    projected = _tool_result_envelope(
        item, body, citation_handle=citation_handle
    )
    if item.item_kind is FrozenProviderInputItemKind.TOOL_RESULT:
        return LLMMessage.tool_result(projected, tool_call_id=item.tool_call_id)
    metadata = item.tool_result_context
    assert metadata is not None
    return LLMMessage.user(
        canonical_json_bytes(
            {
                "pulsara_late_tool_outcome": {
                    "result": json.loads(projected)["pulsara_tool_result"],
                    "tool_call_id": item.tool_call_id,
                }
            }
        ).decode("utf-8")
    )


def _tool_result_envelope(
    item: FrozenProviderInputItem,
    body: str,
    *,
    citation_handle: str | None,
) -> str:
    metadata = item.tool_result_context
    if metadata is None:
        raise ValueError("tool result observation metadata is absent")
    timing = metadata.timing
    payload = {
        "pulsara_tool_result": {
            "body": _project_plan_tool_result_if_owned(body, timing.observation_origin),
            "citation_handle": citation_handle,
            "model_visible_memory_ids": list(
                metadata.model_visible_memory_fact_ids
            ),
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
            "result_state": metadata.result_state,
        }
    }
    encoded = canonical_json_bytes(payload)
    # Timing is essential and has a small closed physical bound even when the
    # selected body is an artifact reference or omitted projection.
    metadata_only = canonical_json_bytes(
        {
            "pulsara_tool_result": {
                "body": "",
                "citation_handle": citation_handle,
                "model_visible_memory_ids": list(
                    metadata.model_visible_memory_fact_ids
                ),
                "observation": payload["pulsara_tool_result"]["observation"],
                "result_state": metadata.result_state,
            }
        }
    )
    if len(metadata_only) > 2 * 1024:
        raise ValueError("tool observation metadata exceeds its physical bound")
    result = encoded.decode("utf-8")
    decode_tool_result_observation(result)
    return result


def decode_tool_result_observation(text: str) -> Mapping[str, object]:
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
        or len(citation_handle.encode("utf-8")) > 128
    ):
        raise ValueError("tool result citation handle is invalid")
    memory_ids = payload["model_visible_memory_ids"]
    if (
        not isinstance(memory_ids, list)
        or len(memory_ids) > 50
        or any(not isinstance(value, str) or not value for value in memory_ids)
        or len(set(memory_ids)) != len(memory_ids)
    ):
        raise ValueError("tool result memory provenance header is invalid")
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
    if duration != normalize_observation_duration(duration):
        raise ValueError("tool timing duration is invalid")
    if reported != normalize_observation_duration(reported):
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


def _project_tool_result_closure(text: str) -> str:
    if "Plan interaction ended before" in text:
        disposition = "PLAN_INTERACTION_ABORTED"
    else:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("tool result closure storage carrier is invalid") from exc
        if not isinstance(value, dict) or not isinstance(
            value.get("disposition"), str
        ):
            raise ValueError("tool result closure disposition is invalid")
        disposition = value["disposition"]
    return canonical_json_bytes({"disposition": disposition}).decode("utf-8")


def _project_plan_tool_result_if_owned(body: str, origin: object) -> str:
    if getattr(origin, "value", origin) != "PLAN_CONTROL":
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


def _project_terminal_observation(text: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("terminal observation storage carrier is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("terminal observation storage carrier is not an object")
    renames = {
        "available_source_utf8_bytes": "available_source_bytes",
        "delivery_coverage": "delivery_coverage",
        "exit_code": "exit_code",
        "gap_before_output": "gap",
        "included_source_utf8_bytes": "included_source_bytes",
        "monitor_id": "monitor_id",
        "observation_kind": "observation_kind",
        "omitted_by_delivery_bound_utf8_bytes": "omitted_source_bytes",
        "output": "output",
        "output_disposition": "display_kind",
        "process_id": "process_id",
        "process_status": "status",
    }
    return {
        projected: value[storage]
        for storage, projected in sorted(renames.items())
        if storage in value
    }


def _project_plan_continuation(text: str) -> str:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Plan continuation storage carrier is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"pulsara_plan_continuation"}:
        raise ValueError("Plan continuation provider carrier is not closed")
    projected = value["pulsara_plan_continuation"]
    if not isinstance(projected, dict):
        raise ValueError("Plan continuation provider payload is not an object")
    transition = str(projected.get("transition") or "")
    if transition not in {"ENTERED_PLAN", "REVISION_REQUESTED", "APPROVED_PLAN"}:
        raise ValueError("Plan continuation transition is invalid")
    expected_keys = {
        "ENTERED_PLAN": {"status", "transition"},
        "REVISION_REQUESTED": {"feedback", "status", "transition"},
        "APPROVED_PLAN": {"status", "transition"},
    }[transition]
    if transition == "APPROVED_PLAN":
        valid_keys = (
            expected_keys,
            {*expected_keys, "approved_plan"},
        )
    else:
        valid_keys = (expected_keys,)
    if set(projected) not in valid_keys:
        raise ValueError("Plan continuation provider members are invalid")
    if projected.get("status") != (
        "APPROVED" if transition == "APPROVED_PLAN" else "ACTIVE"
    ):
        raise ValueError("Plan continuation provider status is invalid")
    if transition == "REVISION_REQUESTED":
        feedback = projected["feedback"]
        if not isinstance(feedback, dict) or set(feedback) not in (
            {"presence"},
            {"presence", "text"},
        ):
            raise ValueError("Plan revision feedback union is invalid")
        if feedback.get("presence") == "ABSENT":
            if set(feedback) != {"presence"}:
                raise ValueError("absent Plan feedback has content")
        elif feedback.get("presence") == "PRESENT":
            if set(feedback) != {"presence", "text"} or not isinstance(
                feedback.get("text"), str
            ):
                raise ValueError("present Plan feedback is invalid")
        else:
            raise ValueError("Plan feedback presence is invalid")
    if "approved_plan" in projected and not isinstance(
        projected["approved_plan"], str
    ):
        raise ValueError("approved Plan body is invalid")
    encoded = canonical_json_bytes(value).decode("utf-8")
    if encoded != text:
        raise ValueError("Plan continuation provider carrier is not canonical")
    return encoded


def _metadata_envelope(item: FrozenProviderInputItem) -> dict[str, object]:
    metadata = item.tool_result_context
    assert metadata is not None
    return {
        "result_state": metadata.result_state,
        "display_kind": metadata.display_kind.value,
        "source_coverage": metadata.source_coverage.value,
        "source_coverage_reason": (
            None
            if metadata.source_coverage_reason is None
            else metadata.source_coverage_reason.value
        ),
        "artifact_disposition": metadata.artifact_disposition.value,
        "artifact_id": metadata.artifact_id,
        "artifact_unavailability_reason": (
            None
            if metadata.artifact_unavailability_reason is None
            else metadata.artifact_unavailability_reason.value
        ),
    }


def _compact_tool_result_body(
    body: str,
    *,
    item: FrozenProviderInputItem,
    maximum_bytes: int,
    artifact_read_available: bool,
) -> str | None:
    encoded = body.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return body
    metadata = item.tool_result_context
    assert metadata is not None
    readable_artifact = (
        artifact_read_available
        and metadata.artifact_id is not None
        and metadata.artifact_disposition
        in {
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolOutputArtifactDisposition.INCOMPLETE,
        }
    )

    def render(retained_byte_budget: int) -> str:
        head_budget = retained_byte_budget // 2
        tail_budget = retained_byte_budget - head_budget
        head = _utf8_prefix(encoded, head_budget)
        tail = _utf8_suffix(encoded, tail_budget)
        head_text = head.decode("utf-8")
        tail_text = tail.decode("utf-8")
        included_bytes = len(head) + len(tail)
        included_characters = len(head_text) + len(tail_text)
        omitted_bytes = max(0, len(encoded) - included_bytes)
        omitted_characters = max(0, len(body) - included_characters)
        envelope = _metadata_envelope(item)
        envelope.update(
            {
                "projection": "COMPACT_HEAD_TAIL",
                "canonical_preview_utf8_bytes": len(encoded),
                "canonical_preview_characters": len(body),
                "included_utf8_bytes": included_bytes,
                "included_characters": included_characters,
                "omitted_utf8_bytes": omitted_bytes,
                "omitted_characters": omitted_characters,
            }
        )
        prefix = (
            "[PULSARA_TOOL_RESULT_COMPACT "
            + json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "]\n"
        )
        marker = (
            "\n[… omitted "
            f"{omitted_bytes} canonical preview UTF-8 bytes / "
            f"{omitted_characters} characters …]\n"
        )
        guidance = (
            "\nUse artifact_read with artifact_id="
            + json.dumps(metadata.artifact_id, ensure_ascii=False)
            + " and paginate with offset/limit."
            if readable_artifact
            else ""
        )
        return prefix + head_text + marker + tail_text + guidance

    low = 0
    high = min(maximum_bytes, len(encoded) - 1)
    winner: str | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = render(middle)
        if len(candidate.encode("utf-8")) <= maximum_bytes:
            winner = candidate
            low = middle + 1
        else:
            high = middle - 1
    return winner


def _bounded_compact_tool_result_body(
    item: FrozenProviderInputItem,
    *,
    maximum_message_bytes: int,
    artifact_read_available: bool,
    citation_handle: str | None,
) -> str | None:
    """Choose the largest deterministic body budget whose final carrier fits."""

    body = item.tool_result_body_text
    if body is None:
        raise ValueError("tool result compact projection lacks a body")
    low = 1
    high = maximum_message_bytes
    winner: str | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = _compact_tool_result_body(
            body,
            item=item,
            maximum_bytes=middle,
            artifact_read_available=artifact_read_available,
        )
        if candidate is None:
            low = middle + 1
            continue
        message = _tool_result_message(
            item, candidate, citation_handle=citation_handle
        )
        if _message_logical_utf8_bytes(message) <= maximum_message_bytes:
            winner = candidate
            low = middle + 1
        else:
            high = middle - 1
    return winner


def _artifact_reference_body(item: FrozenProviderInputItem) -> str:
    metadata = item.tool_result_context
    assert metadata is not None and metadata.artifact_id is not None
    warning = (
        "This artifact starts at the retained snapshot and cannot recover bytes "
        "lost before retention."
        if metadata.artifact_disposition is ToolOutputArtifactDisposition.INCOMPLETE
        else "The complete sanitized tool output is available."
    )
    return (
        "[PULSARA_TOOL_RESULT_REFERENCE]\n"
        + json.dumps(
            _metadata_envelope(item),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        + warning
        + " Use artifact_read with artifact_id="
        + json.dumps(metadata.artifact_id, ensure_ascii=False)
        + " and paginate with offset/limit.\n"
        "[/PULSARA_TOOL_RESULT_REFERENCE]"
    )


def _omitted_tool_result_body(item: FrozenProviderInputItem) -> str:
    return (
        "[PULSARA_TOOL_RESULT_BODY_OMITTED]\n"
        + json.dumps(
            _metadata_envelope(item),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\nThe result outcome is known; only its provider projection body was omitted.\n"
        "[/PULSARA_TOOL_RESULT_BODY_OMITTED]"
    )


def _utf8_prefix(value: bytes, maximum: int) -> bytes:
    candidate = value[:maximum]
    while candidate:
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError:
            candidate = candidate[:-1]
    return b""


def _utf8_suffix(value: bytes, maximum: int) -> bytes:
    candidate = value[max(0, len(value) - maximum) :]
    while candidate:
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError:
            candidate = candidate[1:]
    return b""


__all__ = [
    "decode_tool_result_observation",
    "LoweredCanonicalItem",
    "LoweredToolResultVariant",
    "lower_canonical_item",
    "source_variant_message",
]
