"""The single v2 model-input and final-wire token estimator."""

from __future__ import annotations

from dataclasses import dataclass
import base64
from typing import TYPE_CHECKING, Callable, Protocol

from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    MessageRole,
    ToolSpec,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.model_call import (
    TokenEstimatorFact,
    canonical_json_bytes,
    sha256_fingerprint,
)

if TYPE_CHECKING:
    from pulsara_agent.model_input.contracts import FrozenToolSpec

TEXT_CHARS_PER_TOKEN = 4
JSON_CHARS_PER_TOKEN = 2
REQUEST_ENVELOPE_TOKENS = 3
SYSTEM_MESSAGE_FRAMING_TOKENS = 4
MESSAGE_FRAMING_TOKENS = 4
TOOL_CALL_FRAMING_TOKENS = 4
TOOL_SPEC_FRAMING_TOKENS = 8
IMAGE_GRID_PIXELS = 28
IMAGE_SCALE_NUMERATOR = 7
IMAGE_SCALE_DENOMINATOR = 8
IMAGE_MIN_TOKENS = 256


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    system_tokens: int
    message_tokens: int
    message_tokens_by_index: tuple[int, ...]
    tool_tokens: int
    envelope_tokens: int
    visual_image_tokens: int
    total_input_tokens: int

    def __post_init__(self) -> None:
        if self.message_tokens != sum(self.message_tokens_by_index):
            raise ValueError("message token total does not match per-message breakdown")
        if self.total_input_tokens != (
            self.system_tokens
            + self.message_tokens
            + self.tool_tokens
            + self.envelope_tokens
        ):
            raise ValueError("total input tokens do not match estimate components")
        if not 0 <= self.visual_image_tokens <= self.total_input_tokens:
            raise ValueError("visual image token estimate is invalid")
        if any(value < 0 for value in self.message_tokens_by_index):
            raise ValueError("message token estimates must be non-negative")

    @property
    def text_and_framing_tokens(self) -> int:
        return self.total_input_tokens - self.visual_image_tokens


@dataclass(frozen=True, slots=True)
class FinalWireTokenEstimate:
    total_input_tokens: int
    visual_image_tokens: int

    def __post_init__(self) -> None:
        if not 0 <= self.visual_image_tokens <= self.total_input_tokens:
            raise ValueError("final-wire token estimate is invalid")

    @property
    def text_and_framing_tokens(self) -> int:
        return self.total_input_tokens - self.visual_image_tokens


class TokenEstimator(Protocol):
    fact: TokenEstimatorFact

    def estimate_text(self, text: str) -> int: ...

    def estimate_json(self, value: object) -> int: ...

    def estimate_wire_json_component(self, value: object) -> int: ...

    def estimate_final_wire_json_components(
        self,
        *,
        fixed_context: object,
        ordered_input_items: tuple[object, ...],
        ordered_input_sources: tuple[LLMMessage | None, ...],
    ) -> FinalWireTokenEstimate: ...

    def estimate_tool_spec(self, tool: ToolSpec) -> int: ...

    def estimate_message(self, message: LLMMessage) -> int: ...

    def estimate_context(self, context: LLMContext) -> TokenEstimate: ...

    def estimate_frozen_tool_spec(self, tool: "FrozenToolSpec") -> int: ...

    def estimate_frozen_input(
        self,
        *,
        system_prompt: str,
        messages: tuple[LLMMessage, ...],
        tools: tuple["FrozenToolSpec", ...],
    ) -> TokenEstimate: ...


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def estimate_image_visual_tokens(*, width: int, height: int) -> int:
    """Apply the frozen D1 estimate to one validated image occurrence."""

    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width < 1
        or height < 1
    ):
        raise ValueError("image dimensions must be positive integers")
    cells = _ceil_div(width, IMAGE_GRID_PIXELS) * _ceil_div(
        height, IMAGE_GRID_PIXELS
    )
    return max(
        IMAGE_MIN_TOKENS,
        _ceil_div(IMAGE_SCALE_NUMERATOR * cells, IMAGE_SCALE_DENOMINATOR),
    )


class PulsaraHeuristicTokenEstimatorV2:
    def __init__(self) -> None:
        payload = {
            "estimator_id": "pulsara_heuristic",
            "estimator_version": "v2",
            "constants": {
                "text_chars_per_token": TEXT_CHARS_PER_TOKEN,
                "json_chars_per_token": JSON_CHARS_PER_TOKEN,
                "request_envelope_tokens": REQUEST_ENVELOPE_TOKENS,
                "system_message_framing_tokens": SYSTEM_MESSAGE_FRAMING_TOKENS,
                "message_framing_tokens": MESSAGE_FRAMING_TOKENS,
                "tool_call_framing_tokens": TOOL_CALL_FRAMING_TOKENS,
                "tool_spec_framing_tokens": TOOL_SPEC_FRAMING_TOKENS,
            },
            "unicode_counting": "python_code_points",
            "canonical_json": "sort_keys,compact,utf8,finite",
            "message_fields": [
                "content",
                # Retain the established estimator identity after removing
                # the superseded semantic-thinking DTO slot.
                "thinking",
                "tool_calls.id",
                "tool_calls.name",
                "tool_calls.arguments",
                "tool_call_id",
                "name",
                "arguments",
            ],
            "breakdown": "per_message_includes_message_and_tool_call_framing",
            "wire_component_traversal": (
                "request_envelope+canonical_fixed_context_json+"
                "sum(message_framing+canonical_ordered_item_json)"
            ),
            "image_input": {
                "grid_pixels": IMAGE_GRID_PIXELS,
                "scale_numerator": IMAGE_SCALE_NUMERATOR,
                "scale_denominator": IMAGE_SCALE_DENOMINATOR,
                "minimum_tokens": IMAGE_MIN_TOKENS,
                "wire_accounting": (
                    "formal_user_image_parts:v1-payload-elided-per-item-rounding"
                ),
            },
        }
        self.fact = TokenEstimatorFact(
            estimator_id="pulsara_heuristic",
            estimator_version="v2",
            image_grid_pixels=IMAGE_GRID_PIXELS,
            image_scale_numerator=IMAGE_SCALE_NUMERATOR,
            image_scale_denominator=IMAGE_SCALE_DENOMINATOR,
            image_min_tokens=IMAGE_MIN_TOKENS,
            image_wire_accounting_contract=(
                "formal_user_image_parts:v1-payload-elided-per-item-rounding"
            ),
            estimator_fingerprint=sha256_fingerprint("token-estimator:v2", payload),
        )

    def estimate_text(self, text: str) -> int:
        return 0 if text == "" else _ceil_div(len(text), TEXT_CHARS_PER_TOKEN)

    def estimate_json(self, value: object) -> int:
        rendered = canonical_json_bytes(value).decode("utf-8")
        return 0 if rendered == "" else _ceil_div(len(rendered), JSON_CHARS_PER_TOKEN)

    def estimate_wire_json_component(self, value: object) -> int:
        """Estimate one already-lowered ordered provider input item.

        This is deliberately a wire-object traversal.  It does not recover an
        ``LLMMessage`` or consult the compiler's per-message estimate.
        """

        return MESSAGE_FRAMING_TOKENS + self.estimate_json(value)

    def estimate_final_wire_json_components(
        self,
        *,
        fixed_context: object,
        ordered_input_items: tuple[object, ...],
        ordered_input_sources: tuple[LLMMessage | None, ...],
    ) -> FinalWireTokenEstimate:
        """Estimate one adapter-owned final context projection additively.

        ``fixed_context`` is the exact adapter projection with its ordered
        input array empty; it therefore owns root placement, native tools,
        profile defaults and container framing.  Every final ordered item is
        then traversed with the same local JSON primitive and message framing.
        The additive seam makes durable replay replacement arithmetic exact.
        """

        if len(ordered_input_items) != len(ordered_input_sources):
            raise ValueError("final-wire items do not align with semantic sources")
        text_and_framing = REQUEST_ENVELOPE_TOKENS + self.estimate_json(
            fixed_context
        )
        visual = 0
        for item, source in zip(
            ordered_input_items, ordered_input_sources, strict=True
        ):
            counting_item, item_visual = _final_wire_counting_item(
                item=item,
                source=source,
            )
            text_and_framing += MESSAGE_FRAMING_TOKENS + self.estimate_json(
                counting_item
            )
            visual += item_visual
        return FinalWireTokenEstimate(
            total_input_tokens=text_and_framing + visual,
            visual_image_tokens=visual,
        )

    def estimate_tool_spec(self, tool: ToolSpec) -> int:
        return TOOL_SPEC_FRAMING_TOKENS + self.estimate_json(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
        )

    def estimate_message(self, message: LLMMessage) -> int:
        total = MESSAGE_FRAMING_TOKENS
        for part in message.content:
            if isinstance(part, LLMImagePart):
                total += estimate_image_visual_tokens(
                    width=part.width,
                    height=part.height,
                )
            elif not isinstance(part, LLMTextPart):
                raise TypeError("model message contains an invalid content part")
            else:
                total += self.estimate_text(part.text)
        for call in message.tool_calls:
            total += TOOL_CALL_FRAMING_TOKENS
            total += self.estimate_text(call.id)
            total += self.estimate_text(call.name)
            total += self.estimate_text(call.arguments)
        for value in (message.tool_call_id, message.name, message.arguments):
            if value is not None:
                total += self.estimate_text(value)
        return total

    def estimate_context(self, context: LLMContext) -> TokenEstimate:
        system_tokens = (
            SYSTEM_MESSAGE_FRAMING_TOKENS + self.estimate_text(context.system_prompt)
            if context.system_prompt
            else 0
        )
        message_tokens_by_index = tuple(
            self.estimate_message(message) for message in context.messages
        )
        message_tokens = sum(message_tokens_by_index)
        visual_image_tokens = sum(
            estimate_image_visual_tokens(width=part.width, height=part.height)
            for message in context.messages
            for part in message.content
            if isinstance(part, LLMImagePart)
        )
        tool_tokens = sum(self.estimate_tool_spec(tool) for tool in context.tools)
        envelope_tokens = REQUEST_ENVELOPE_TOKENS
        return TokenEstimate(
            system_tokens=system_tokens,
            message_tokens=message_tokens,
            message_tokens_by_index=message_tokens_by_index,
            tool_tokens=tool_tokens,
            envelope_tokens=envelope_tokens,
            visual_image_tokens=visual_image_tokens,
            total_input_tokens=(
                system_tokens + message_tokens + tool_tokens + envelope_tokens
            ),
        )

    def estimate_frozen_input_cooperative(
        self,
        *,
        system_prompt: str,
        messages: tuple[LLMMessage, ...],
        tools: tuple["FrozenToolSpec", ...],
        checkpoint: Callable[[], None],
    ) -> TokenEstimate:
        """Estimate the same contract while yielding at bounded item seams."""

        checkpoint()
        system_tokens = (
            SYSTEM_MESSAGE_FRAMING_TOKENS + self.estimate_text(system_prompt)
            if system_prompt
            else 0
        )
        message_tokens_by_index: list[int] = []
        for message in messages:
            checkpoint()
            message_tokens_by_index.append(self.estimate_message(message))
        tool_tokens = 0
        for tool in tools:
            checkpoint()
            tool_tokens += self.estimate_frozen_tool_spec(tool)
        checkpoint()
        message_tokens = sum(message_tokens_by_index)
        visual_image_tokens = sum(
            estimate_image_visual_tokens(width=part.width, height=part.height)
            for message in messages
            for part in message.content
            if isinstance(part, LLMImagePart)
        )
        return TokenEstimate(
            system_tokens=system_tokens,
            message_tokens=message_tokens,
            message_tokens_by_index=tuple(message_tokens_by_index),
            tool_tokens=tool_tokens,
            envelope_tokens=REQUEST_ENVELOPE_TOKENS,
            visual_image_tokens=visual_image_tokens,
            total_input_tokens=(
                system_tokens + message_tokens + tool_tokens + REQUEST_ENVELOPE_TOKENS
            ),
        )

    def estimate_frozen_tool_spec(self, tool: "FrozenToolSpec") -> int:
        parameters = thaw_json(tool.parameters)
        if not isinstance(parameters, dict):
            raise TypeError("frozen tool schema did not thaw to an object")
        return self.estimate_tool_spec(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=parameters,
            )
        )

    def estimate_frozen_input(
        self,
        *,
        system_prompt: str,
        messages: tuple[LLMMessage, ...],
        tools: tuple["FrozenToolSpec", ...],
    ) -> TokenEstimate:
        system_tokens = (
            SYSTEM_MESSAGE_FRAMING_TOKENS + self.estimate_text(system_prompt)
            if system_prompt
            else 0
        )
        message_tokens_by_index = tuple(
            self.estimate_message(message) for message in messages
        )
        message_tokens = sum(message_tokens_by_index)
        visual_image_tokens = sum(
            estimate_image_visual_tokens(width=part.width, height=part.height)
            for message in messages
            for part in message.content
            if isinstance(part, LLMImagePart)
        )
        tool_tokens = sum(self.estimate_frozen_tool_spec(tool) for tool in tools)
        return TokenEstimate(
            system_tokens=system_tokens,
            message_tokens=message_tokens,
            message_tokens_by_index=message_tokens_by_index,
            tool_tokens=tool_tokens,
            envelope_tokens=REQUEST_ENVELOPE_TOKENS,
            visual_image_tokens=visual_image_tokens,
            total_input_tokens=(
                system_tokens + message_tokens + tool_tokens + REQUEST_ENVELOPE_TOKENS
            ),
        )


def estimate_model_context_for_call(
    *, call: object, context: LLMContext
) -> TokenEstimate:
    """PR1 estimate-only seam; validation is layered around this in PR3."""

    target = getattr(call, "target", None)
    estimator = getattr(target, "token_estimator", None)
    if estimator is None:
        raise TypeError("resolved model call does not carry a token estimator")
    return estimator.estimate_context(context)


def _final_wire_counting_item(
    *,
    item: object,
    source: LLMMessage | None,
) -> tuple[object, int]:
    """Return the D1 token-counting view for one exact ordered wire item."""

    source_has_image = source is not None and any(
        isinstance(part, LLMImagePart) for part in source.content
    )
    if not source_has_image:
        if _has_formal_user_image_wire_part(item):
            raise ValueError(
                "formal image wire item lacks its validated semantic source"
            )
        return item, 0
    assert source is not None
    if source.role is not MessageRole.USER or not isinstance(item, dict):
        raise ValueError("formal image wire item lacks its USER semantic source")
    content = item.get("content")
    if not isinstance(content, list) or len(content) != len(source.content):
        raise ValueError("formal image parts do not align with semantic content")
    image_wire_types = {
        part.get("type")
        for part, semantic_part in zip(content, source.content, strict=True)
        if isinstance(semantic_part, LLMImagePart) and isinstance(part, dict)
    }
    if image_wire_types == {"image_url"}:
        return _chat_image_counting_item(item=item, source=source)
    if image_wire_types == {"input_image"}:
        return _responses_image_counting_item(item=item, source=source)
    raise ValueError("formal image wire group has an invalid protocol shape")


def _has_formal_user_image_wire_part(item: object) -> bool:
    if not isinstance(item, dict) or item.get("role") != "user":
        return False
    content = item.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict)
        and part.get("type") in {"image_url", "input_image"}
        for part in content
    )


def _chat_image_counting_item(
    *, item: dict[object, object], source: LLMMessage
) -> tuple[object, int]:
    if set(item) != {"role", "content"} or item.get("role") != "user":
        raise ValueError("Chat formal image item has an invalid USER shape")
    content = item.get("content")
    if not isinstance(content, list) or len(content) != len(source.content):
        raise ValueError("Chat formal image parts do not align with semantic content")
    counted: list[dict[str, object]] = []
    visual = 0
    for wire_part, source_part in zip(content, source.content, strict=True):
        if not isinstance(wire_part, dict):
            raise ValueError("Chat formal image content part is not an object")
        if isinstance(source_part, LLMTextPart):
            expected = {"type": "text", "text": source_part.text}
            if wire_part != expected:
                raise ValueError("Chat text part differs from its semantic source")
            counted.append(expected)
            continue
        if not isinstance(source_part, LLMImagePart):
            raise TypeError("Chat semantic content contains an invalid part")
        if set(wire_part) != {"type", "image_url"} or wire_part.get(
            "type"
        ) != "image_url":
            raise ValueError("Chat formal image part has an invalid shape")
        image_url = wire_part.get("image_url")
        if (
            not isinstance(image_url, dict)
            or set(image_url) != {"url", "detail"}
            or image_url.get("detail") != "auto"
        ):
            raise ValueError("Chat formal image URL has an invalid shape")
        prefix = _validated_inline_image_url(
            image_url.get("url"), image=source_part
        )
        counted.append(
            {
                "type": "image_url",
                "image_url": {"url": prefix, "detail": "auto"},
            }
        )
        visual += estimate_image_visual_tokens(
            width=source_part.width, height=source_part.height
        )
    return {"role": "user", "content": counted}, visual


def _responses_image_counting_item(
    *, item: dict[object, object], source: LLMMessage
) -> tuple[object, int]:
    if set(item) != {"role", "content"} or item.get("role") != "user":
        raise ValueError("Responses formal image item has an invalid USER shape")
    content = item.get("content")
    if not isinstance(content, list) or len(content) != len(source.content):
        raise ValueError(
            "Responses formal image parts do not align with semantic content"
        )
    counted: list[dict[str, object]] = []
    visual = 0
    for wire_part, source_part in zip(content, source.content, strict=True):
        if not isinstance(wire_part, dict):
            raise ValueError("Responses formal image content part is not an object")
        if isinstance(source_part, LLMTextPart):
            expected = {"type": "input_text", "text": source_part.text}
            if wire_part != expected:
                raise ValueError("Responses text part differs from its semantic source")
            counted.append(expected)
            continue
        if not isinstance(source_part, LLMImagePart):
            raise TypeError("Responses semantic content contains an invalid part")
        if (
            set(wire_part) != {"type", "image_url", "detail"}
            or wire_part.get("type") != "input_image"
            or wire_part.get("detail") != "auto"
        ):
            raise ValueError("Responses formal image part has an invalid shape")
        prefix = _validated_inline_image_url(
            wire_part.get("image_url"), image=source_part
        )
        counted.append(
            {"type": "input_image", "image_url": prefix, "detail": "auto"}
        )
        visual += estimate_image_visual_tokens(
            width=source_part.width, height=source_part.height
        )
    return {"role": "user", "content": counted}, visual


def _validated_inline_image_url(value: object, *, image: LLMImagePart) -> str:
    prefix = f"data:{image.media_type};base64,"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("formal image URL differs from its validated MIME")
    payload = value[len(prefix) :]
    expected_length = 4 * _ceil_div(len(image.immutable_bytes), 3)
    if len(payload) != expected_length:
        raise ValueError("formal image base64 length differs from its source")
    payload_offset = 0
    chunk_bytes = 3 * 8192
    for source_offset in range(0, len(image.immutable_bytes), chunk_bytes):
        encoded = base64.b64encode(
            memoryview(image.immutable_bytes)[source_offset : source_offset + chunk_bytes]
        ).decode("ascii")
        if payload[payload_offset : payload_offset + len(encoded)] != encoded:
            raise ValueError("formal image base64 differs from its source bytes")
        payload_offset += len(encoded)
    if payload_offset != len(payload):
        raise ValueError("formal image base64 has trailing content")
    return prefix
