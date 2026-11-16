"""Typed content and source facts for one accepted Cell output occurrence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from pulsara_agent.identity import require_id
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMTextPart,
    llm_content_identity_value,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    CanonicalPromptImageDescriptor,
)
from pulsara_agent.ports.tool_execution import freeze_tool_json_object
from pulsara_agent.primitives.context import canonical_json_bytes

CELL_OUTPUT_MEDIA_TYPE = "application/vnd.pulsara.terminal-observation+json"
CELL_OUTPUT_CODEC = "utf-8"


def cell_output_provider_text(text: str) -> str:
    """Escape isolated JS code units only at the provider boundary."""
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def cell_output_canonical_body(parts) -> tuple[bytes, int]:
    """Quote projected occurrences from immutable images or metadata only."""
    from pulsara_agent.conversation_kernel.prompt_content import (
        CanonicalPromptImageDescriptor,
    )

    values, image_bytes = [], 0
    for part in parts:
        if isinstance(part, LLMTextPart):
            values.append({"type": "text", "text": part.text})
        else:
            descriptor = isinstance(part, CanonicalPromptImageDescriptor)
            size = part.encoded_bytes if descriptor else len(part.immutable_bytes)
            image_bytes += size
            values.append(
                {
                    "type": "image",
                    "image_ref": part.image_ref,
                    "digest": part.digest if descriptor else part.content_digest,
                    "encoded_bytes": size,
                    "media_type": part.media_type,
                    "width": part.width,
                    "height": part.height,
                }
            )
    body = canonical_json_bytes({"parts": values})
    return body, len(body) + image_bytes


@dataclass(frozen=True, slots=True)
class CellOutputResourceQuote:
    canonical_expanded_bytes: int


@dataclass(frozen=True, slots=True)
class CellOutputSource:
    cell_id: str
    record_sequence: int
    kind: str

    def __post_init__(self):
        require_id(self.cell_id, "cell")
        if type(self.record_sequence) is not int or self.record_sequence < 1:
            raise ValueError("Cell output record sequence is invalid")
        if self.kind not in {"log", "return", "diagnostic", "tool_result"}:
            raise ValueError("Cell output source kind is invalid")


@dataclass(frozen=True, slots=True)
class CellOutputImageReadFailure(CanonicalPromptImageDescriptor):
    """One payload-read observation; immutable accepted image facts are intact."""

    reason: str

    def __post_init__(self):
        CanonicalPromptImageDescriptor.__post_init__(self)
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Cell image read failure requires its actual reason")


def cell_output_identity_value(parts):
    """Commit accepted image facts independently of a temporary payload read."""
    values = []
    for part in parts:
        if isinstance(part, CellOutputImageReadFailure):
            values.append(
                {
                    "type": "image",
                    "digest": part.digest,
                    "image_ref": part.image_ref,
                    "encoded_bytes": part.encoded_bytes,
                    "media_type": part.media_type,
                    "width": part.width,
                    "height": part.height,
                }
            )
        else:
            values.extend(llm_content_identity_value((part,)))
    return tuple(values)


@dataclass(frozen=True, slots=True)
class CellOutputProviderContent:
    parts: tuple[LLMTextPart | LLMImagePart | CellOutputImageReadFailure, ...]


@dataclass(frozen=True, slots=True)
class CellOutputContent:
    """One normalized output, retained by the existing lease until acceptance."""

    cell_id: str
    source_turn_id: str
    source_exec_attempt_id: str
    record_sequence: int
    record: Mapping[str, object] = field(repr=False)
    images: tuple[LLMImagePart | CellOutputImageReadFailure, ...] = field(
        default=(), repr=False
    )
    body: bytes = field(init=False, repr=False)

    def __post_init__(self):
        require_id(self.cell_id, "cell")
        require_id(self.source_turn_id, "turn")
        require_id(self.source_exec_attempt_id, "attempt")
        if type(self.record_sequence) is not int or self.record_sequence < 1:
            raise ValueError("Cell output record sequence is invalid")
        record = freeze_tool_json_object(self.record)
        if set(record) != {"kind", "content"} or record["kind"] not in {
            "log",
            "return",
            "diagnostic",
            "tool_result",
        }:
            raise ValueError(
                "Cell output requires normalized content and its source kind"
            )
        if not isinstance(record["content"], (tuple, list)):
            raise ValueError("Cell output content must be ordered")
        for part in record["content"]:
            if not isinstance(part, Mapping) or set(part) != {"type", "value"}:
                raise ValueError("Cell output content item is invalid")
            if part["type"] == "text" and isinstance(part["value"], str):
                continue
            if (
                part["type"] == "image"
                and isinstance(part["value"], Mapping)
                and set(part["value"]) == {"kind", "image_ref"}
                and part["value"]["kind"] == "IMAGE_REF"
            ):
                require_id(part["value"]["image_ref"], "image")
                continue
            raise ValueError("Cell outputs contain only text and image occurrences")
        object.__setattr__(self, "record", record)
        object.__setattr__(
            self, "body", cell_output_canonical_body(self.content.parts)[0]
        )

    @property
    def source(self) -> CellOutputSource:
        return CellOutputSource(self.cell_id, self.record_sequence, self.storage_kind)

    @property
    def storage_kind(self):
        return self.record["kind"]

    @property
    def storage_parts(self):
        return self.record["content"]

    @property
    def content(self):
        images = {image.image_ref: image for image in self.images}
        return CellOutputProviderContent(
            tuple(
                LLMTextPart(cell_output_provider_text(part["value"]))
                if part["type"] == "text"
                else images[part["value"]["image_ref"]]
                for part in self.storage_parts
            )
        )

    @property
    def resource_quote(self):
        return CellOutputResourceQuote(
            cell_output_canonical_body(self.content.parts)[1]
        )

    @property
    def image_occurrences(self):
        from pulsara_agent.conversation_kernel.prompt_content import (
            CanonicalPromptImageOccurrence,
        )

        return tuple(
            CanonicalPromptImageOccurrence(index, part_index, image)
            for index, (part_index, image) in enumerate(
                (i, part)
                for i, part in enumerate(self.content.parts)
                if isinstance(part, LLMImagePart)
            )
        )
