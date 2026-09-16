"""Small provider-neutral message and prompt-content vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any

ALLOWED_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAXIMUM_PROMPT_IMAGE_PIXELS = 16_777_216
MAXIMUM_PROMPT_TEXT_UTF8_BYTES = 1 << 20


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True, slots=True)
class LLMTextPart:
    """One exact text occurrence shared by ingress and model input."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("model text part must contain text")
        self.text.encode("utf-8")


@dataclass(frozen=True, slots=True)
class PromptImagePart:
    """Caller-provided image bytes before trusted image validation."""

    original_bytes: bytes = field(repr=False)
    declared_mime: str

    def __post_init__(self) -> None:
        if not isinstance(self.original_bytes, bytes):
            raise TypeError("prompt image bytes must be immutable bytes")
        if not self.original_bytes:
            raise ValueError("prompt image bytes cannot be empty")
        if (
            not isinstance(self.declared_mime, str)
            or self.declared_mime not in ALLOWED_IMAGE_MEDIA_TYPES
        ):
            raise ValueError("prompt image MIME is outside the supported input set")


@dataclass(frozen=True, slots=True)
class LLMImagePart:
    """One validated, immutable image occurrence used by model input."""

    media_type: str
    immutable_bytes: bytes = field(repr=False)
    width: int
    height: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.media_type, str)
            or self.media_type not in ALLOWED_IMAGE_MEDIA_TYPES
        ):
            raise ValueError("model image MIME is outside the validated input set")
        if not isinstance(self.immutable_bytes, bytes):
            raise TypeError("model image payload must be immutable bytes")
        if not self.immutable_bytes:
            raise ValueError("model image payload cannot be empty")
        if (
            isinstance(self.width, bool)
            or isinstance(self.height, bool)
            or not isinstance(self.width, int)
            or not isinstance(self.height, int)
            or self.width < 1
            or self.height < 1
            or self.width * self.height > MAXIMUM_PROMPT_IMAGE_PIXELS
        ):
            raise ValueError("model image dimensions are outside the validated range")

    @property
    def content_digest(self) -> str:
        return "sha256:" + sha256(self.immutable_bytes).hexdigest()


LLMContentPart = LLMTextPart | LLMImagePart
PromptContentPart = LLMTextPart | PromptImagePart


def prompt_text_utf8_bytes(parts: tuple[LLMTextPart, ...]) -> int:
    """Validate the inherited prompt-text contract and return its byte charge."""

    if not isinstance(parts, tuple) or any(
        not isinstance(part, LLMTextPart) for part in parts
    ):
        raise TypeError("prompt text must be an immutable tuple of text parts")
    total = 0
    for part in parts:
        if any(
            character not in "\n\t" and ord(character) < 0x20 for character in part.text
        ):
            raise ValueError("prompt contains a forbidden control character")
        total += len(part.text.encode("utf-8"))
    if total > MAXIMUM_PROMPT_TEXT_UTF8_BYTES:
        raise ValueError("prompt text exceeds its UTF-8 byte bound")
    return total


def _validate_prompt_parts(
    parts: tuple[PromptContentPart, ...] | tuple[LLMContentPart, ...],
    *,
    image_type: type[PromptImagePart] | type[LLMImagePart],
) -> None:
    if not isinstance(parts, tuple):
        raise TypeError("prompt content parts must be an immutable tuple")
    if not parts:
        raise ValueError("prompt content cannot be empty")
    if any(not isinstance(part, (LLMTextPart, image_type)) for part in parts):
        raise TypeError("prompt content contains an invalid part")
    if not any(
        isinstance(part, image_type)
        or (isinstance(part, LLMTextPart) and part.text != "")
        for part in parts
    ):
        raise ValueError("prompt content must contain text or an image")
    prompt_text_utf8_bytes(
        tuple(part for part in parts if isinstance(part, LLMTextPart))
    )


@dataclass(frozen=True, slots=True)
class PromptContent:
    """One ordered caller submission before image validation and freezing."""

    parts: tuple[PromptContentPart, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _validate_prompt_parts(self.parts, image_type=PromptImagePart)

    @classmethod
    def text(cls, text: str) -> "PromptContent":
        return cls((LLMTextPart(text),))


@dataclass(frozen=True, slots=True)
class FrozenPromptContent:
    """One validated ordered prompt value ready for canonical ownership."""

    parts: tuple[LLMContentPart, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _validate_prompt_parts(self.parts, image_type=LLMImagePart)

    @classmethod
    def text(cls, text: str) -> "FrozenPromptContent":
        return cls((LLMTextPart(text),))


def content_has_image(parts: tuple[LLMContentPart, ...]) -> bool:
    return any(isinstance(part, LLMImagePart) for part in parts)


def text_part_values(parts: tuple[LLMContentPart, ...]) -> tuple[str, ...]:
    """Return exact text values only when the complete content is text-only."""

    values: list[str] = []
    for part in parts:
        if not isinstance(part, LLMTextPart):
            raise ValueError("text-only consumer cannot consume image content")
        values.append(part.text)
    return tuple(values)


def join_text_content(
    parts: tuple[LLMContentPart, ...], *, separator: str = "\n"
) -> str:
    return separator.join(text_part_values(parts))


def prompt_text_projection(
    content: PromptContent | FrozenPromptContent,
) -> str:
    """Project only Text parts for Hook-style advisory consumers."""

    return "\n".join(
        part.text for part in content.parts if isinstance(part, LLMTextPart)
    )


def frozen_tool_result_public_text(content: FrozenPromptContent) -> str:
    """Project image content to bounded text for Hooks and Tool-role output."""

    if not isinstance(content, FrozenPromptContent):
        raise TypeError("ToolResult public projection requires frozen content")
    images = tuple(part for part in content.parts if isinstance(part, LLMImagePart))
    if len(images) != 1:
        raise ValueError("ToolResult public projection requires one image")
    image = images[0]
    return (
        f"Image loaded ({image.media_type}, {image.width}x{image.height}, "
        f"{len(image.immutable_bytes)} bytes)."
    )


def llm_content_identity_value(
    parts: tuple[LLMContentPart, ...],
) -> tuple[object, ...]:
    """Serialize complete content while preserving existing text-only identity."""

    values: list[object] = []
    for part in parts:
        if isinstance(part, LLMTextPart):
            values.append(part.text)
        elif isinstance(part, LLMImagePart):
            values.append(
                {
                    "type": "image",
                    "digest": part.content_digest,
                    "encoded_bytes": len(part.immutable_bytes),
                    "media_type": part.media_type,
                    "width": part.width,
                    "height": part.height,
                }
            )
        else:
            raise TypeError("model content identity requires typed parts")
    return tuple(values)


def frozen_prompt_content_canonical_value(
    content: FrozenPromptContent,
) -> dict[str, object]:
    """Return the canonical descriptor-only value for one frozen prompt.

    The low-level typed snapshot carrier uses this same value as the Kernel
    prompt codec, so the carrier can remain a provider-input value without a
    dependency back into the conversation repository.
    """

    if not isinstance(content, FrozenPromptContent):
        raise TypeError("canonical prompt value requires frozen prompt content")
    parts: list[dict[str, object]] = []
    for part in content.parts:
        if isinstance(part, LLMTextPart):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, LLMImagePart):
            parts.append(
                {
                    "type": "image",
                    "digest": part.content_digest,
                    "encoded_bytes": len(part.immutable_bytes),
                    "media_type": part.media_type,
                    "width": part.width,
                    "height": part.height,
                }
            )
        else:  # pragma: no cover - FrozenPromptContent closes this union.
            raise TypeError("canonical prompt value requires typed parts")
    return {"schema": "pulsara.prompt/v1", "parts": tuple(parts)}


def llm_content_logical_bytes(parts: tuple[LLMContentPart, ...]) -> int:
    """Measure the frozen provider-neutral L contract for content parts."""

    total = 0
    for part in parts:
        if isinstance(part, LLMTextPart):
            total += len(part.text.encode("utf-8"))
        elif isinstance(part, LLMImagePart):
            total += len(part.immutable_bytes) + len(part.media_type.encode("utf-8"))
        else:
            raise TypeError("model logical size requires typed parts")
    return total


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str = "{}"


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One complete provider input item; it owns no runtime lifecycle state."""

    role: MessageRole
    content: tuple[LLMContentPart, ...] = field(default_factory=tuple)
    tool_calls: tuple[LLMToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None
    name: str | None = None
    arguments: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            raise TypeError("model message role must be closed")
        if not isinstance(self.content, tuple) or any(
            not isinstance(part, (LLMTextPart, LLMImagePart)) for part in self.content
        ):
            raise TypeError("model message content must be typed immutable parts")
        if self.role is not MessageRole.USER and content_has_image(self.content):
            raise ValueError("image content is only valid in USER messages")
        if self.role in {MessageRole.SYSTEM, MessageRole.USER} and (
            self.tool_calls
            or self.tool_call_id is not None
            or self.name is not None
            or self.arguments is not None
        ):
            raise ValueError("textual provider message has tool-only fields")

    @classmethod
    def system(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.SYSTEM, content=(LLMTextPart(text),))

    @classmethod
    def user(
        cls,
        text: str,
    ) -> "LLMMessage":
        # Occurrence attribution belongs to canonical conversation rows.  It is
        # intentionally not duplicated into the process-local provider carrier.
        return cls(role=MessageRole.USER, content=(LLMTextPart(text),))

    @classmethod
    def user_content(cls, content: FrozenPromptContent) -> "LLMMessage":
        if not isinstance(content, FrozenPromptContent):
            raise TypeError("USER content must be frozen prompt content")
        return cls(role=MessageRole.USER, content=content.parts)

    @classmethod
    def assistant(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.ASSISTANT, content=(LLMTextPart(text),))

    @classmethod
    def assistant_turn(
        cls,
        *,
        text: str | None = None,
        tool_calls: tuple[LLMToolCall, ...] = (),
    ) -> "LLMMessage":
        content = (LLMTextPart(text),) if text else ()
        return cls(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
        )

    @classmethod
    def tool_call(cls, *, tool_call_id: str, name: str, arguments: str) -> "LLMMessage":
        return cls(
            role=MessageRole.TOOL_CALL,
            tool_call_id=tool_call_id,
            name=name,
            arguments=arguments,
        )

    @classmethod
    def tool_result(cls, text: str, *, tool_call_id: str | None = None) -> "LLMMessage":
        return cls(
            role=MessageRole.TOOL_RESULT,
            content=(LLMTextPart(text),),
            tool_call_id=tool_call_id,
        )


__all__ = [
    "ALLOWED_IMAGE_MEDIA_TYPES",
    "FrozenPromptContent",
    "LLMContentPart",
    "LLMImagePart",
    "LLMMessage",
    "LLMTextPart",
    "LLMToolCall",
    "MAXIMUM_PROMPT_IMAGE_PIXELS",
    "MAXIMUM_PROMPT_TEXT_UTF8_BYTES",
    "MessageRole",
    "PromptContent",
    "PromptContentPart",
    "PromptImagePart",
    "ToolSpec",
    "content_has_image",
    "join_text_content",
    "llm_content_identity_value",
    "llm_content_logical_bytes",
    "prompt_text_projection",
    "prompt_text_utf8_bytes",
    "text_part_values",
]
