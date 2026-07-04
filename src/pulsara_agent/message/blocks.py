"""Message content blocks for Pulsara runtime events."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
    id: str = Field(default_factory=lambda: uuid4().hex)


class ThinkingBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["thinking"] = "thinking"
    thinking: str
    id: str = Field(default_factory=lambda: uuid4().hex)


class Base64Source(BaseModel):
    type: Literal["base64"] = "base64"
    data: str
    media_type: str


class URLSource(BaseModel):
    type: Literal["url"] = "url"
    url: str
    media_type: str


class DataBlock(BaseModel):
    type: Literal["data"] = "data"
    source: Base64Source | URLSource
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str | None = None


class HintBlock(BaseModel):
    type: Literal["hint"] = "hint"
    hint: str | list[TextBlock | DataBlock]
    id: str = Field(default_factory=lambda: uuid4().hex)
    source: str | None = None


class ToolCallState(StrEnum):
    PENDING = "pending"
    ASKING = "asking"
    ALLOWED = "allowed"
    SUBMITTED = "submitted"
    FINISHED = "finished"


class ToolCallBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    input: str = ""
    state: ToolCallState = ToolCallState.PENDING
    suggested_rules: list[dict] = Field(default_factory=list)


class ToolResultState(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    DENIED = "denied"
    RUNNING = "running"


class ToolResultPreviewMetadata(BaseModel):
    """Durable metadata for a model-facing preview of a retained artifact."""

    preview_policy: Literal["full", "head_tail", "head_tail_huge"]
    preview_chars: int
    original_chars: int
    original_bytes: int
    omitted_middle_chars: int
    visible_head_chars: int
    visible_tail_chars: int
    read_more: dict[str, object] = Field(default_factory=dict)


class ToolResultArtifactRef(BaseModel):
    """Structured reference to a persisted artifact produced by a tool result."""

    artifact_id: str
    role: str
    media_type: str
    size_bytes: int
    stored_complete: bool = True
    loss_reason: str | None = None
    preview: ToolResultPreviewMetadata | None = None

    def to_model_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "artifact_id": self.artifact_id,
            "role": self.role,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "stored_complete": self.stored_complete,
            "read_more": {"tool": "artifact_read"},
        }
        if self.loss_reason is not None:
            payload["loss_reason"] = self.loss_reason
        if self.preview is not None:
            payload["preview"] = self.preview.model_dump()
        return payload


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    id: str
    name: str
    output: list[TextBlock | DataBlock] = Field(default_factory=list)
    state: ToolResultState = ToolResultState.RUNNING
    artifacts: list[ToolResultArtifactRef] = Field(default_factory=list)


ContentBlock = (
    TextBlock
    | ThinkingBlock
    | HintBlock
    | ToolCallBlock
    | ToolResultBlock
    | DataBlock
)
