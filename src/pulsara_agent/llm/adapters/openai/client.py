"""Shared OpenAI SDK client helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


OPENAI_RESPONSES_API = "openai_responses"
OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"


def build_async_openai_client(
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: float,
) -> AsyncOpenAI:
    """Create an AsyncOpenAI client for a model profile."""

    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout=timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class OpenAIAdapterError(Exception):
    message: str
    provider_data: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def provider_error_data(exc: BaseException) -> dict[str, Any]:
    """Return small, serializable provider error metadata."""

    data: dict[str, Any] = {"type": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        data["status_code"] = status_code
    response = getattr(exc, "response", None)
    if response is not None:
        data["response"] = str(response)
    body = getattr(exc, "body", None)
    if body is not None:
        data["body"] = body
    return data
