"""The single v3 model-input and final-wire token estimator."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import re
import unicodedata
from typing import Protocol

from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    MessageRole,
)
from pulsara_agent.primitives.model_call import (
    TokenEstimatorFact,
    canonical_json_bytes,
    sha256_fingerprint,
)

TEXT_UTF8_BYTES_PER_TOKEN = 4
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_CJK_NAME_PREFIXES = (
    "CJK ",
    "IDEOGRAPHIC ",
    "HIRAGANA ",
    "KATAKANA ",
    "KATAKANA-HIRAGANA ",
    "HANGUL ",
    "BOPOMOFO ",
    "HALFWIDTH KATAKANA ",
    "HALFWIDTH HANGUL ",
)
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

    def estimate_ordered_wire_json_components(
        self,
        *,
        ordered_input_items: tuple[object, ...],
        ordered_input_sources: tuple[LLMMessage | None, ...],
    ) -> FinalWireTokenEstimate: ...

    def estimate_final_wire_json_components(
        self,
        *,
        fixed_context: object,
        ordered_input_items: tuple[object, ...],
        ordered_input_sources: tuple[LLMMessage | None, ...],
    ) -> FinalWireTokenEstimate: ...


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


class PulsaraHeuristicTokenEstimatorV3:
    def __init__(self) -> None:
        payload = {
            "estimator_id": "pulsara_heuristic",
            "estimator_version": "v3",
            "constants": {
                "text_utf8_bytes_per_token": TEXT_UTF8_BYTES_PER_TOKEN,
                "request_envelope_tokens": REQUEST_ENVELOPE_TOKENS,
                "system_message_framing_tokens": SYSTEM_MESSAGE_FRAMING_TOKENS,
                "message_framing_tokens": MESSAGE_FRAMING_TOKENS,
                "tool_call_framing_tokens": TOOL_CALL_FRAMING_TOKENS,
                "tool_spec_framing_tokens": TOOL_SPEC_FRAMING_TOKENS,
            },
            "unicode_counting": {
                "formula": "ceil(non_cjk_utf8_bytes/4+cjk_code_points)",
                "cjk_name_prefixes": _CJK_NAME_PREFIXES,
                "wide_punctuation": "category:P*,east_asian_width:W|F",
                "unicode_data_version": unicodedata.unidata_version,
                "json_text_rule": "same_as_plain_text",
            },
            "canonical_json": "sort_keys,compact,utf8,finite",
            "message_fields": [
                "content",
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
            estimator_version="v3",
            image_grid_pixels=IMAGE_GRID_PIXELS,
            image_scale_numerator=IMAGE_SCALE_NUMERATOR,
            image_scale_denominator=IMAGE_SCALE_DENOMINATOR,
            image_min_tokens=IMAGE_MIN_TOKENS,
            image_wire_accounting_contract=(
                "formal_user_image_parts:v1-payload-elided-per-item-rounding"
            ),
            estimator_fingerprint=sha256_fingerprint("token-estimator:v3", payload),
        )

    def estimate_text(self, text: str) -> int:
        # Python owns the Unicode names/categories. Pulsara counts CJK text
        # and wide punctuation as one token per code point; all other text
        # contributes its UTF-8 bytes divided by four, rounded once per item.
        weighted_bytes = len(text.encode("utf-8"))
        for match in _NON_ASCII.finditer(text):
            character = match.group()
            if unicodedata.name(character, "").startswith(_CJK_NAME_PREFIXES) or (
                unicodedata.category(character).startswith("P")
                and unicodedata.east_asian_width(character) in {"W", "F"}
            ):
                weighted_bytes += TEXT_UTF8_BYTES_PER_TOKEN - len(
                    character.encode("utf-8")
                )
        return _ceil_div(weighted_bytes, TEXT_UTF8_BYTES_PER_TOKEN)

    def estimate_json(self, value: object) -> int:
        rendered = canonical_json_bytes(value).decode("utf-8")
        return self.estimate_text(rendered)

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

        ordered = self.estimate_ordered_wire_json_components(
            ordered_input_items=ordered_input_items,
            ordered_input_sources=ordered_input_sources,
        )
        fixed = REQUEST_ENVELOPE_TOKENS + self.estimate_json(fixed_context)
        return FinalWireTokenEstimate(
            total_input_tokens=fixed + ordered.total_input_tokens,
            visual_image_tokens=ordered.visual_image_tokens,
        )

    def estimate_ordered_wire_json_components(
        self,
        *,
        ordered_input_items: tuple[object, ...],
        ordered_input_sources: tuple[LLMMessage | None, ...],
    ) -> FinalWireTokenEstimate:
        """Estimate exact ordered wire items with their semantic image sources."""

        if len(ordered_input_items) != len(ordered_input_sources):
            raise ValueError("wire items do not align with semantic sources")
        text_and_framing = 0
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
