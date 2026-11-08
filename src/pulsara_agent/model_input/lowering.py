"""Provider-neutral lowering for canonical conversation facts."""

from __future__ import annotations

from pulsara_agent.identity import require_id
from pulsara_agent.primitives.permission import PermissionMode

from dataclasses import dataclass
import json
from typing import Mapping

from pulsara_agent.llm.input import (
    LLMContentPart,
    prompt_provider_parts,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    LLMToolCall,
    MessageRole,
    text_part_values,
    image_reference_text,
)
from pulsara_agent.model_input.contracts import (
    CompactionSnapshotCarrier,
    ContextChannel,
    ContextRenderMode,
    ContextSourceCandidate,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    provider_input_item_text,
    ToolResultProviderRenderMode,
    StructuredModelInputCompileError,
    ModelInputCompileFailureKind,
)
from pulsara_agent.model_input.continuity import (
    SourceObservationLifecycle,
    SourceObservationPresence,
    encode_runtime_observation,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_projection import (
    ToolResultLogicalMessageKind,
    decode_provider_tool_result_observation,
    provider_neutral_message_logical_bytes,
    project_tool_result_storage_body,
    render_provider_tool_result_logical_message,
    _maximum_tool_result_page_timing,
)


@dataclass(frozen=True, slots=True)
class LoweredToolResultVariant:
    mode: ToolResultProviderRenderMode
    message: LLMMessage
    utf8_bytes: int

    def __post_init__(self) -> None:
        if self.utf8_bytes != provider_neutral_message_logical_bytes(self.message):
            raise ValueError("tool-result variant logical quote mismatch")
        if (
            self.mode is ToolResultProviderRenderMode.FULL
            and self.utf8_bytes > MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
        ):
            raise ValueError("FULL tool-result variant exceeds its logical bound")


@dataclass(frozen=True, slots=True)
class LoweredCanonicalItem:
    source: FrozenProviderInputItem
    fixed_message: LLMMessage | None
    tool_result_variants: tuple[LoweredToolResultVariant, ...] = ()

    def __post_init__(self) -> None:
        is_tool_result = self.source.item_kind in {
            FrozenProviderInputItemKind.TOOL_RESULT,
            FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        }
        if is_tool_result:
            if self.fixed_message is not None or not self.tool_result_variants:
                raise ValueError("lowered canonical item carrier union is invalid")
        elif self.fixed_message is None or self.tool_result_variants:
            raise ValueError("lowered canonical item carrier union is invalid")
        if self.tool_result_variants and tuple(
            item.mode for item in self.tool_result_variants
        ) != (ToolResultProviderRenderMode.FULL,):
            raise ValueError("frozen tool projection requires one FULL representation")


@dataclass(frozen=True, slots=True)
class ProjectedToolResultPublicValue:
    render_mode: ToolResultProviderRenderMode
    value: object
    body: str = ""


def image_reference_id_part(image_ref: str) -> LLMTextPart:
    """Render one provider-only label from the canonical image digest value."""

    require_id(image_ref, "image")
    return LLMTextPart(
        canonical_json_bytes(
            {"pulsara_image": {"target": {"kind": "IMAGE_REF", "image_ref": image_ref}}}
        ).decode("utf-8")
    )


def image_reference_part(image: LLMImagePart) -> LLMTextPart:
    """Render the single provider-only reference label for an image occurrence."""

    if not isinstance(image, LLMImagePart):
        raise TypeError("image reference label requires a validated image")
    return image_reference_id_part(image.image_ref)


def image_omission_part(image: LLMImagePart) -> LLMTextPart:
    """Keep an image's identity without its visual content or a read directive."""
    return LLMTextPart(
        image_reference_text(
            image_ref=image.image_ref,
            media_type=image.media_type,
            width=image.width,
            height=image.height,
        )
    )


def image_referenced_content(
    parts: tuple[LLMContentPart, ...],
) -> tuple[LLMContentPart, ...]:
    """Insert one deterministic provider-only label before every actual image."""

    rendered: list[LLMContentPart] = []
    for part in parts:
        if isinstance(part, LLMImagePart):
            rendered.append(image_reference_part(part))
        rendered.append(part)
    return tuple(rendered)


def project_tool_result_public_value(
    item: FrozenProviderInputItem,
) -> ProjectedToolResultPublicValue:
    """Return the frozen native body for Hook/compiler reuse."""

    lowered = lower_canonical_item(item)
    if not lowered.tool_result_variants:
        raise ValueError("public ToolResult projection requires a ToolResult item")
    selected = lowered.tool_result_variants[0]
    payload = decode_provider_tool_result_observation(
        text_part_values(selected.message.content)[0]
    )
    body = payload["body"]
    if not isinstance(body, str):
        raise TypeError("public ToolResult body is not text")
    try:
        value: object = json.loads(body)
    except json.JSONDecodeError:
        value = body
    return ProjectedToolResultPublicValue(selected.mode, value, body)


def lower_canonical_item(
    item: FrozenProviderInputItem,
) -> LoweredCanonicalItem:
    kind = item.item_kind
    if kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT:
        if not isinstance(item.content, CompactionSnapshotCarrier):
            raise TypeError("context snapshot item lacks its typed carrier")
        return LoweredCanonicalItem(
            item,
            LLMMessage(
                role=MessageRole.USER,
                content=compaction_snapshot_provider_content(item.content),
            ),
        )
    if kind is FrozenProviderInputItemKind.USER:
        if not isinstance(item.content, tuple):
            raise TypeError("USER item content must contain typed parts")
        return LoweredCanonicalItem(
            item,
            LLMMessage(
                role=MessageRole.USER,
                content=scheduled_request_parts(item.scheduled_input)
                + image_referenced_content(prompt_provider_parts(item.content)),
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
            ),
        )
    text = provider_input_item_text(item)
    if kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION:
        return LoweredCanonicalItem(item, LLMMessage.user(_terminal_observation_provider_text(text)))
    if kind is FrozenProviderInputItemKind.PLAN_CONTINUATION:
        return LoweredCanonicalItem(
            item,
            LLMMessage.user(_project_plan_continuation(text)),
        )
    if kind is FrozenProviderInputItemKind.ASSISTANT:
        return LoweredCanonicalItem(item, LLMMessage.assistant(text))
    if kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
        return LoweredCanonicalItem(
            item,
            LLMMessage.assistant_turn(
                text=text or None,
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
                _project_tool_result_closure(text),
                tool_call_id=item.tool_call_id,
            ),
        )
    raise TypeError(kind)


def scheduled_request_parts(provenance) -> tuple[LLMContentPart, ...]:
    if provenance is None:
        return ()
    from pulsara_agent.scheduling.contracts import ScheduledTriggerSource
    from pulsara_agent.scheduling.requests import schedule_target

    source_text = {
        ScheduledTriggerSource.TIMER: "Automatic timer",
        ScheduledTriggerSource.RUN_ONCE: "User selected Run once",
    }[provenance.trigger_source]
    return (
        LLMTextPart(
            f"Saved task ({source_text}; requested at {provenance.due_at_utc}; "
            f"target: {canonical_json_bytes(schedule_target(provenance.task_id)).decode('utf-8')}). Execute the saved instruction:\n"
        ),
    )


def display_json(value: object) -> str:
    """Render a canonical JSON value for temporary summary source material."""

    return canonical_json_bytes(value).decode("utf-8")


def compaction_snapshot_display_header(carrier: CompactionSnapshotCarrier) -> str:
    if not isinstance(carrier, CompactionSnapshotCarrier):
        raise TypeError("snapshot provider projection requires a typed carrier")
    return "The following is a summary of earlier history."


def compaction_snapshot_provider_content(
    carrier: CompactionSnapshotCarrier,
) -> tuple[LLMContentPart, ...]:
    """Render only the explanation and S; canonical R/N lower independently."""

    return (
        LLMTextPart(
            compaction_snapshot_display_header(carrier)
            + "\n\n"
            + carrier.earlier_context_summary
        ),
    )


def source_variant_message(
    candidate: ContextSourceCandidate,
    text: str,
    *,
    mode: ContextRenderMode | None = None,
) -> LLMMessage:
    if candidate.channel is ContextChannel.SYSTEM:
        raise ValueError("SYSTEM source is not an ordered message")
    selected_mode = mode
    if selected_mode is None:
        matches = tuple(
            variant.mode for variant in candidate.variants if variant.text == text
        )
        selected_mode = matches[0] if len(matches) == 1 else ContextRenderMode.FULL
    if selected_mode is ContextRenderMode.UNAVAILABLE_MINIMAL:
        if text:
            raise ValueError("minimal unavailable source has a non-empty body")
        return encode_runtime_observation(
            source_kind=candidate.source_kind,
            trust_class=candidate.trust_class,
            lifecycle=SourceObservationLifecycle.UNAVAILABLE,
            presence=SourceObservationPresence.UNAVAILABLE,
            contract_version=candidate.source_contract_version,
            body="",
        )
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


def ordinary_tool_result_minimum_upper(
    tool_call_id: str, *, body: str, model_visible_memory_fact_ids: tuple[str, ...] = (),
) -> LLMMessage:
    """Quote a known necessary receipt before execution, never an invented preview.

    The caller owns the actual receipt facts. This function only covers their
    fixed carrier, closed timing maximum and normal/late delivery. Business
    owners still validate the actual minimum before any physical effect.
    """
    messages = tuple(render_provider_tool_result_logical_message(
        message_kind=kind,
        tool_call_id=tool_call_id,
        body=body,
        result_state="CANCELLED_BEFORE_DISPATCH",
        timing=_maximum_tool_result_page_timing(),
        model_visible_memory_ids=model_visible_memory_fact_ids,
    ).message for kind in ToolResultLogicalMessageKind)
    return max(messages, key=lambda message: len(canonical_json_bytes(
        text_part_values(message.content)[0]
    )))


def _tool_result_variants(
    item: FrozenProviderInputItem,
) -> tuple[LoweredToolResultVariant, ...]:
    if (item.tool_result_context is None or item.tool_result_body_text is None
            or item.tool_call_id is None):
        raise ValueError("tool result lowering lacks typed metadata")
    message = _tool_result_message(item, item.tool_result_body_text)
    logical_bytes = provider_neutral_message_logical_bytes(message)
    if logical_bytes > MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE
        )
    return (LoweredToolResultVariant(
        ToolResultProviderRenderMode.FULL, message, logical_bytes,
    ),)


def _tool_result_message(
    item: FrozenProviderInputItem,
    body: str,
) -> LLMMessage:
    assert item.tool_call_id is not None
    metadata = item.tool_result_context
    assert metadata is not None
    projected_body = project_tool_result_storage_body(
        body, metadata.timing.observation_origin
    )
    rendered = render_provider_tool_result_logical_message(
        message_kind=(
            ToolResultLogicalMessageKind.TOOL_RESULT
            if item.item_kind is FrozenProviderInputItemKind.TOOL_RESULT
            else ToolResultLogicalMessageKind.LATE_TOOL_OUTCOME
        ),
        tool_call_id=item.tool_call_id,
        body=projected_body,
        result_state=metadata.result_state,
        timing=metadata.timing,
        model_visible_memory_ids=metadata.model_visible_memory_fact_ids,
    )
    return rendered.message


def decode_tool_result_observation(text: str) -> Mapping[str, object]:
    return decode_provider_tool_result_observation(text)


def _project_tool_result_closure(text: str) -> str:
    if "Plan interaction ended before" in text:
        disposition = "PLAN_INTERACTION_ABORTED"
    else:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("tool result closure storage carrier is invalid") from exc
        if not isinstance(value, dict) or not isinstance(value.get("disposition"), str):
            raise ValueError("tool result closure disposition is invalid")
        disposition = value["disposition"]
    return canonical_json_bytes({"disposition": disposition}).decode("utf-8")


def _terminal_observation_provider_text(text: str) -> str:
    from pulsara_agent.ports.terminal_observation import CellOutputContent
    value = CellOutputContent.from_bytes(text.encode("utf-8"))
    # This validated canonical JSON already preserves JavaScript lone UTF-16
    # surrogates through escapes. Embed its exact bytes; decoding to Python
    # scalars and re-encoding would either reject them or rewrite the body.
    return '{"pulsara_terminal_observation":' + value.canonical_bytes().decode("utf-8") + '}'


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
    if transition not in {"ENTERED_PLAN", "APPROVED_PLAN", "EXITED_PLAN"}:
        raise ValueError("Plan continuation transition is invalid")
    expected_keys = {
        "ENTERED_PLAN": {"status", "transition"},
        "EXITED_PLAN": {"status", "transition", "resume_permission_mode"},
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
    if (
        projected.get("status")
        != (
            {
                "APPROVED_PLAN": "APPROVED",
                "ENTERED_PLAN": "ACTIVE",
                "EXITED_PLAN": "EXITED",
            }[transition]
        )
    ):
        raise ValueError("Plan continuation provider status is invalid")
    if transition == "EXITED_PLAN":
        PermissionMode(projected.get("resume_permission_mode"))
    if "approved_plan" in projected and not isinstance(projected["approved_plan"], str):
        raise ValueError("approved Plan body is invalid")
    encoded = canonical_json_bytes(value).decode("utf-8")
    if encoded != text:
        raise ValueError("Plan continuation provider carrier is not canonical")
    return encoded


__all__ = [
    "decode_tool_result_observation",
    "image_reference_id_part",
    "image_reference_part",
    "image_referenced_content",
    "LoweredCanonicalItem",
    "LoweredToolResultVariant",
    "lower_canonical_item",
    "source_variant_message",
]
