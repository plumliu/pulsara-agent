"""Pure canonical prompt-content codec and resource calculations.

Repository publication, reference rows, image decoding, and Host admission
remain with their existing owners.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from typing import Mapping

from pulsara_agent.llm.estimator import estimate_image_visual_tokens
from pulsara_agent.llm.input import (
    ALLOWED_IMAGE_MEDIA_TYPES,
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
    MAXIMUM_PROMPT_IMAGE_PIXELS,
    MAXIMUM_PROMPT_TEXT_UTF8_BYTES,
    frozen_prompt_content_canonical_value,
    llm_content_logical_bytes,
    prompt_text_utf8_bytes,
)
from pulsara_agent.model_input.contracts import (
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
)
from pulsara_agent.primitives.context import canonical_json_bytes


PROMPT_BODY_SCHEMA = "pulsara.prompt/v1"
PROMPT_BODY_MEDIA_TYPE = "application/vnd.pulsara.prompt+json"
PROMPT_BODY_CODEC = "utf-8"
PROMPT_IMAGE_BLOB_CODEC = "binary"
MAXIMUM_PROMPT_MULTIPART_BYTES = MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES

_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class CanonicalPromptImageDescriptor:
    digest: str
    encoded_bytes: int
    media_type: str
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or not _SHA256_DIGEST_RE.fullmatch(
            self.digest
        ):
            raise ValueError("canonical prompt image digest is invalid")
        if (
            isinstance(self.encoded_bytes, bool)
            or not isinstance(self.encoded_bytes, int)
            or self.encoded_bytes < 1
        ):
            raise ValueError("canonical prompt image byte length is invalid")
        if (
            not isinstance(self.media_type, str)
            or self.media_type not in ALLOWED_IMAGE_MEDIA_TYPES
        ):
            raise ValueError("canonical prompt image MIME is invalid")
        if (
            isinstance(self.width, bool)
            or isinstance(self.height, bool)
            or not isinstance(self.width, int)
            or not isinstance(self.height, int)
            or self.width < 1
            or self.height < 1
            or self.width * self.height > MAXIMUM_PROMPT_IMAGE_PIXELS
        ):
            raise ValueError("canonical prompt image dimensions are invalid")


CanonicalPromptBodyPart = LLMTextPart | CanonicalPromptImageDescriptor


@dataclass(frozen=True, slots=True)
class CanonicalPromptBody:
    parts: tuple[CanonicalPromptBodyPart, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.parts, tuple):
            raise TypeError("canonical prompt body parts must be an immutable tuple")
        if not self.parts:
            raise ValueError("canonical prompt body must contain parts")
        if any(
            not isinstance(part, (LLMTextPart, CanonicalPromptImageDescriptor))
            for part in self.parts
        ):
            raise TypeError("canonical prompt body contains an invalid part")
        if not any(
            isinstance(part, CanonicalPromptImageDescriptor)
            or (isinstance(part, LLMTextPart) and part.text != "")
            for part in self.parts
        ):
            raise ValueError("canonical prompt body has no content")
        prompt_text_utf8_bytes(
            tuple(part for part in self.parts if isinstance(part, LLMTextPart))
        )


@dataclass(frozen=True, slots=True)
class CanonicalPromptImageOccurrence:
    """One body Image occurrence and its owner-local reference ordinal."""

    ref_ordinal: int
    part_index: int
    image: LLMImagePart = field(repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.ref_ordinal, bool)
            or isinstance(self.part_index, bool)
            or not isinstance(self.ref_ordinal, int)
            or not isinstance(self.part_index, int)
            or self.ref_ordinal < 0
            or self.part_index < 0
            or not isinstance(self.image, LLMImagePart)
        ):
            raise ValueError("canonical image occurrence coordinates are invalid")


@dataclass(frozen=True, slots=True)
class PromptContentResourceQuote:
    """K1 units for one complete prompt; no field is a generic byte total."""

    text_utf8_bytes: int
    canonical_body_bytes: int
    image_occurrence_encoded_bytes: int
    multipart_bytes: int
    canonical_expanded_bytes: int
    epoch_logical_bytes: int
    image_occurrences: int
    visual_image_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.text_utf8_bytes,
            self.canonical_body_bytes,
            self.image_occurrence_encoded_bytes,
            self.multipart_bytes,
            self.canonical_expanded_bytes,
            self.epoch_logical_bytes,
            self.image_occurrences,
            self.visual_image_tokens,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("prompt resource quote contains an invalid value")
        if self.multipart_bytes != (
            self.canonical_body_bytes + self.image_occurrence_encoded_bytes
        ):
            raise ValueError("prompt multipart quote is inconsistent")
        if self.canonical_expanded_bytes != self.multipart_bytes:
            raise ValueError("single-prompt canonical expanded quote is inconsistent")
        if self.text_utf8_bytes > MAXIMUM_PROMPT_TEXT_UTF8_BYTES:
            raise ValueError("prompt resource quote exceeds the text bound")
        if self.multipart_bytes > MAXIMUM_PROMPT_MULTIPART_BYTES:
            raise ValueError("prompt resource quote exceeds the multipart bound")


@dataclass(frozen=True, slots=True)
class FrozenCanonicalPrompt:
    """Exact frozen content plus its deterministic body/ref/resource contract."""

    content: FrozenPromptContent = field(repr=False)
    body: bytes = field(repr=False)
    image_occurrences: tuple[CanonicalPromptImageOccurrence, ...] = field(repr=False)
    resource_quote: PromptContentResourceQuote

    def __post_init__(self) -> None:
        if not isinstance(self.content, FrozenPromptContent):
            raise TypeError("canonical prompt content must be frozen")
        if not isinstance(self.body, bytes):
            raise TypeError("canonical prompt body must be immutable bytes")
        if not isinstance(self.image_occurrences, tuple) or any(
            not isinstance(item, CanonicalPromptImageOccurrence)
            for item in self.image_occurrences
        ):
            raise TypeError("canonical prompt occurrences must be an immutable tuple")
        if not isinstance(self.resource_quote, PromptContentResourceQuote):
            raise TypeError("canonical prompt resource quote is invalid")
        if self.body != canonical_prompt_body_bytes(self.content):
            raise ValueError("canonical prompt body differs from frozen content")
        if self.image_occurrences != canonical_prompt_image_occurrences(self.content):
            raise ValueError("canonical prompt image occurrences drifted")
        if self.resource_quote != prompt_content_resource_quote(self.content):
            raise ValueError("canonical prompt resource quote drifted")


def _descriptor_for_image(image: LLMImagePart) -> CanonicalPromptImageDescriptor:
    return CanonicalPromptImageDescriptor(
        digest=image.content_digest,
        encoded_bytes=len(image.immutable_bytes),
        media_type=image.media_type,
        width=image.width,
        height=image.height,
    )


def _part_value(
    part: LLMTextPart | LLMImagePart | CanonicalPromptImageDescriptor,
) -> dict[str, object]:
    if isinstance(part, LLMTextPart):
        return {"type": "text", "text": part.text}
    descriptor = _descriptor_for_image(part) if isinstance(part, LLMImagePart) else part
    return {
        "type": "image",
        "digest": descriptor.digest,
        "encoded_bytes": descriptor.encoded_bytes,
        "media_type": descriptor.media_type,
        "width": descriptor.width,
        "height": descriptor.height,
    }


def canonical_prompt_body_value(
    value: FrozenPromptContent | CanonicalPromptBody,
) -> dict[str, object]:
    if isinstance(value, FrozenPromptContent):
        return frozen_prompt_content_canonical_value(value)
    return {
        "schema": PROMPT_BODY_SCHEMA,
        "parts": tuple(_part_value(part) for part in value.parts),
    }


def canonical_prompt_body_bytes(content: FrozenPromptContent) -> bytes:
    return canonical_json_bytes(canonical_prompt_body_value(content))


def canonical_prompt_body(content: FrozenPromptContent) -> CanonicalPromptBody:
    """Project frozen content to its descriptor-only canonical value."""

    if not isinstance(content, FrozenPromptContent):
        raise TypeError("canonical prompt body requires frozen prompt content")
    return CanonicalPromptBody(
        tuple(
            part if isinstance(part, LLMTextPart) else _descriptor_for_image(part)
            for part in content.parts
        )
    )


def canonical_prompt_image_occurrences(
    content: FrozenPromptContent,
) -> tuple[CanonicalPromptImageOccurrence, ...]:
    occurrences: list[CanonicalPromptImageOccurrence] = []
    for part_index, part in enumerate(content.parts):
        if isinstance(part, LLMImagePart):
            occurrences.append(
                CanonicalPromptImageOccurrence(
                    ref_ordinal=len(occurrences),
                    part_index=part_index,
                    image=part,
                )
            )
    return tuple(occurrences)


def canonical_prompt_body_resource_quote(
    body: CanonicalPromptBody,
) -> PromptContentResourceQuote:
    """Quote descriptor metadata before any image payload is hydrated."""

    if not isinstance(body, CanonicalPromptBody):
        raise TypeError("canonical prompt resource quote requires a decoded body")
    body_bytes = canonical_json_bytes(canonical_prompt_body_value(body))
    images = tuple(
        part for part in body.parts if isinstance(part, CanonicalPromptImageDescriptor)
    )
    image_bytes = sum(part.encoded_bytes for part in images)
    multipart_bytes = len(body_bytes) + image_bytes
    return PromptContentResourceQuote(
        text_utf8_bytes=prompt_text_utf8_bytes(
            tuple(part for part in body.parts if isinstance(part, LLMTextPart))
        ),
        canonical_body_bytes=len(body_bytes),
        image_occurrence_encoded_bytes=image_bytes,
        multipart_bytes=multipart_bytes,
        canonical_expanded_bytes=multipart_bytes,
        epoch_logical_bytes=sum(
            len(part.text.encode("utf-8"))
            if isinstance(part, LLMTextPart)
            else part.encoded_bytes + len(part.media_type.encode("utf-8"))
            for part in body.parts
        ),
        image_occurrences=len(images),
        visual_image_tokens=sum(
            estimate_image_visual_tokens(
                width=image.width,
                height=image.height,
            )
            for image in images
        ),
    )


def prompt_content_resource_quote(
    content: FrozenPromptContent,
) -> PromptContentResourceQuote:
    quote = canonical_prompt_body_resource_quote(canonical_prompt_body(content))
    if quote.epoch_logical_bytes != llm_content_logical_bytes(content.parts):
        raise ValueError("canonical prompt logical byte quote drifted")
    return quote


def freeze_canonical_prompt(content: FrozenPromptContent) -> FrozenCanonicalPrompt:
    if not isinstance(content, FrozenPromptContent):
        raise TypeError("canonical prompt requires frozen prompt content")
    return FrozenCanonicalPrompt(
        content=content,
        body=canonical_prompt_body_bytes(content),
        image_occurrences=canonical_prompt_image_occurrences(content),
        resource_quote=prompt_content_resource_quote(content),
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("canonical prompt JSON contains a duplicate key")
        result[key] = value
    return result


def decode_canonical_prompt_body(body: bytes) -> CanonicalPromptBody:
    if not isinstance(body, bytes):
        raise TypeError("canonical prompt body must be immutable bytes")
    if len(body) > MAXIMUM_PROMPT_MULTIPART_BYTES:
        raise ValueError("canonical prompt body exceeds its physical bound")
    try:
        text = body.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canonical prompt body is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"schema", "parts"}:
        raise ValueError("canonical prompt body shape is invalid")
    if value["schema"] != PROMPT_BODY_SCHEMA or not isinstance(value["parts"], list):
        raise ValueError("canonical prompt body schema is invalid")
    parts: list[CanonicalPromptBodyPart] = []
    for item in value["parts"]:
        if not isinstance(item, Mapping) or not isinstance(item.get("type"), str):
            raise ValueError("canonical prompt part shape is invalid")
        if item["type"] == "text":
            if set(item) != {"type", "text"} or not isinstance(item["text"], str):
                raise ValueError("canonical prompt text part is invalid")
            parts.append(LLMTextPart(item["text"]))
            continue
        if item["type"] == "image":
            expected = {
                "type",
                "digest",
                "encoded_bytes",
                "media_type",
                "width",
                "height",
            }
            if set(item) != expected:
                raise ValueError("canonical prompt image part is invalid")
            try:
                descriptor = CanonicalPromptImageDescriptor(
                    digest=item["digest"],
                    encoded_bytes=item["encoded_bytes"],
                    media_type=item["media_type"],
                    width=item["width"],
                    height=item["height"],
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "canonical prompt image descriptor is invalid"
                ) from exc
            parts.append(descriptor)
            continue
        raise ValueError("canonical prompt part type is unknown")
    decoded = CanonicalPromptBody(tuple(parts))
    if canonical_json_bytes(canonical_prompt_body_value(decoded)) != body:
        raise ValueError("canonical prompt body is not canonically encoded")
    return decoded


def hydrate_canonical_prompt_body(
    *, body: bytes, image_payloads: tuple[bytes, ...]
) -> FrozenCanonicalPrompt:
    """Join ordered refs/payloads to descriptors and verify immutable identity."""

    if not isinstance(image_payloads, tuple) or any(
        not isinstance(payload, bytes) for payload in image_payloads
    ):
        raise TypeError("canonical prompt image payloads must be immutable bytes")
    decoded = decode_canonical_prompt_body(body)
    descriptor_quote = canonical_prompt_body_resource_quote(decoded)
    expected_images = sum(
        isinstance(part, CanonicalPromptImageDescriptor) for part in decoded.parts
    )
    if len(image_payloads) != expected_images:
        raise ValueError("canonical prompt image refs are incomplete or excessive")
    payload_index = 0
    hydrated: list[LLMTextPart | LLMImagePart] = []
    for part in decoded.parts:
        if isinstance(part, LLMTextPart):
            hydrated.append(part)
            continue
        payload = image_payloads[payload_index]
        payload_index += 1
        if (
            len(payload) != part.encoded_bytes
            or "sha256:" + sha256(payload).hexdigest() != part.digest
        ):
            raise ValueError("canonical prompt image payload identity mismatch")
        hydrated.append(
            LLMImagePart(
                media_type=part.media_type,
                immutable_bytes=payload,
                width=part.width,
                height=part.height,
            )
        )
    result = freeze_canonical_prompt(FrozenPromptContent(tuple(hydrated)))
    if result.body != body:
        raise ValueError("hydrated canonical prompt body drifted")
    if result.resource_quote != descriptor_quote:
        raise ValueError("hydrated canonical prompt resource quote drifted")
    return result


__all__ = [
    "CanonicalPromptBody",
    "CanonicalPromptBodyPart",
    "CanonicalPromptImageDescriptor",
    "CanonicalPromptImageOccurrence",
    "FrozenCanonicalPrompt",
    "MAXIMUM_PROMPT_MULTIPART_BYTES",
    "PROMPT_BODY_CODEC",
    "PROMPT_BODY_MEDIA_TYPE",
    "PROMPT_BODY_SCHEMA",
    "PROMPT_IMAGE_BLOB_CODEC",
    "PromptContentResourceQuote",
    "canonical_prompt_body_bytes",
    "canonical_prompt_body",
    "canonical_prompt_body_resource_quote",
    "canonical_prompt_body_value",
    "canonical_prompt_image_occurrences",
    "decode_canonical_prompt_body",
    "freeze_canonical_prompt",
    "hydrate_canonical_prompt_body",
    "prompt_content_resource_quote",
]
