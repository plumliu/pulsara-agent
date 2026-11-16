"""Provider-neutral lowering for canonical conversation facts."""

from __future__ import annotations

from pulsara_agent.identity import require_id

from dataclasses import dataclass
import json

from pulsara_agent.llm.input import (
    LLMContentPart,
    prompt_provider_parts,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    LLMToolCall,
    MessageRole,
    image_reference_text,
)
from pulsara_agent.model_input.contracts import (
    CompactionSnapshotCarrier,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    provider_input_item_text,
)

from pulsara_agent.primitives.context import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class LoweredCanonicalItem:
    source: FrozenProviderInputItem
    message: LLMMessage


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


def lower_canonical_item(
    item: FrozenProviderInputItem,
    *,
    supports_images: bool = True,
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
        if item.tool_result_body_text is None or item.tool_result_context is None or item.tool_call_id is None:
            raise ValueError("tool result lowering lacks its frozen observation")
        return LoweredCanonicalItem(item,_tool_result_message(item,item.tool_result_body_text))
    if kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION:
        from pulsara_agent.ports.terminal_observation import CellOutputImageReadFailure, cell_output_provider_text
        from pulsara_agent.conversation_kernel.native_tool_delivery import IMAGE_INPUT_UNSUPPORTED_TEXT

        source = item.cell_output_source
        if source is None:
            raise ValueError("Cell output lowering lost its source facts")
        parts = [LLMTextPart(
            f"Cell {source.cell_id} 输出（{source.kind}，第 {source.record_sequence} 条）：\n"
        )]
        for part in item.content:
            if isinstance(part, LLMTextPart):
                parts.append(LLMTextPart(cell_output_provider_text(part.text)))
            else:
                parts.append(image_reference_id_part(part.image_ref))
                if not supports_images:
                    parts.append(LLMTextPart(IMAGE_INPUT_UNSUPPORTED_TEXT))
                elif isinstance(part, CellOutputImageReadFailure):
                    parts.append(LLMTextPart(part.reason))
                else:
                    parts.append(part)
        return LoweredCanonicalItem(item,LLMMessage(role=MessageRole.USER,content=tuple(parts)))
    if kind is FrozenProviderInputItemKind.PLAN_CONTINUATION:
        raise ValueError("internal Plan control entries are not provider messages")
    text = provider_input_item_text(item)
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




def _tool_result_message(item: FrozenProviderInputItem, body: str) -> LLMMessage:
    assert item.tool_call_id is not None
    if item.item_kind is FrozenProviderInputItemKind.TOOL_RESULT:
        return LLMMessage.tool_result(body, tool_call_id=item.tool_call_id)
    return LLMMessage.user("tool_call_id: " + item.tool_call_id + "\n" + body)


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



__all__ = [
    "image_reference_id_part",
    "image_reference_part",
    "image_referenced_content",
    "LoweredCanonicalItem",
    "lower_canonical_item",
]
