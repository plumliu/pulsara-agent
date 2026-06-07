"""Runtime message objects rebuilt from AgentEvent streams."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from pulsara_agent.message.blocks import ContentBlock, DataBlock, TextBlock


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class Msg(BaseModel):
    role: Literal["user", "assistant", "system", "tool_result"]
    name: str
    content: list[ContentBlock] = Field(default_factory=list)
    id: str = Field(default_factory=lambda: uuid4().hex)
    metadata: dict = Field(default_factory=dict)
    created_at: str | None = None
    finished_at: str | None = None
    usage: Usage | None = None


def UserMsg(
    name: str,
    content: str | list[TextBlock | DataBlock],
    *,
    id: str | None = None,
    metadata: dict | None = None,
    created_at: str | None = None,
    finished_at: str | None = None,
) -> Msg:
    blocks = [TextBlock(text=content)] if isinstance(content, str) else list(content)
    return Msg(
        role="user",
        name=name,
        content=blocks,
        id=id or uuid4().hex,
        metadata=metadata or {},
        created_at=created_at,
        finished_at=finished_at,
    )


def AssistantMsg(
    name: str,
    content: str | list[ContentBlock],
    *,
    id: str | None = None,
    metadata: dict | None = None,
    created_at: str | None = None,
    finished_at: str | None = None,
    usage: Usage | None = None,
) -> Msg:
    blocks = [TextBlock(text=content)] if isinstance(content, str) else list(content)
    return Msg(
        role="assistant",
        name=name,
        content=blocks,
        id=id or uuid4().hex,
        metadata=metadata or {},
        created_at=created_at,
        finished_at=finished_at,
        usage=usage,
    )


def SystemMsg(
    name: str,
    content: str,
    *,
    id: str | None = None,
    metadata: dict | None = None,
    created_at: str | None = None,
    finished_at: str | None = None,
) -> Msg:
    return Msg(
        role="system",
        name=name,
        content=[TextBlock(text=content)],
        id=id or uuid4().hex,
        metadata=metadata or {},
        created_at=created_at,
        finished_at=finished_at,
    )
