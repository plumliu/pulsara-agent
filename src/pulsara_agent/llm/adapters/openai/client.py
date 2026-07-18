"""Shared OpenAI SDK client helpers."""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI


OPENAI_RESPONSES_API = "openai_responses"
OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"
def build_async_openai_client(
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: float,
    max_retries: int | None = None,
) -> AsyncOpenAI:
    """Create an AsyncOpenAI client for a model profile."""

    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "timeout": timeout_seconds,
    }
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return AsyncOpenAI(
        **kwargs,
    )
