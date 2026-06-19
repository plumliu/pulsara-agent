"""Shared OpenAI SDK client helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


OPENAI_RESPONSES_API = "openai_responses"
OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"
_MAX_ERROR_VALUE_CHARS = 2_000
_MAX_EXCEPTION_CHAIN_DEPTH = 5


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

    data = _exception_summary(exc)
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        data["status_code"] = status_code
    request = getattr(exc, "request", None)
    if request is not None:
        data["request"] = _safe_string(request)
    response = getattr(exc, "response", None)
    if response is not None:
        data["response"] = _safe_string(response)
    body = getattr(exc, "body", None)
    if body is not None:
        data["body"] = _json_safe(body)
    if isinstance(exc, OpenAIAdapterError) and exc.provider_data:
        data["adapter_provider_data"] = _json_safe(exc.provider_data)
    causes = _exception_causes(exc)
    if causes:
        data["causes"] = causes
    return data


def _exception_summary(exc: BaseException) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": _safe_string(exc),
        "repr": _safe_repr(exc),
    }


def _exception_causes(exc: BaseException) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    seen = {id(exc)}
    current = exc
    for _ in range(_MAX_EXCEPTION_CHAIN_DEPTH):
        relation = "cause"
        next_exc = current.__cause__
        if next_exc is None:
            relation = "context"
            next_exc = current.__context__
        if next_exc is None:
            break
        if id(next_exc) in seen:
            causes.append({"relation": relation, "cycle": True})
            break
        seen.add(id(next_exc))
        summary = _exception_summary(next_exc)
        summary["relation"] = relation
        causes.append(summary)
        current = next_exc
    else:
        causes.append({"truncated": True, "max_depth": _MAX_EXCEPTION_CHAIN_DEPTH})
    return causes


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return _safe_string(value) if isinstance(value, str) else value
    if isinstance(value, dict):
        return {
            _safe_string(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return _safe_string(value)


def _safe_string(value: Any) -> str:
    text = str(value)
    if len(text) <= _MAX_ERROR_VALUE_CHARS:
        return text
    return f"{text[:_MAX_ERROR_VALUE_CHARS]}... [truncated]"


def _safe_repr(value: Any) -> str:
    text = repr(value)
    if len(text) <= _MAX_ERROR_VALUE_CHARS:
        return text
    return f"{text[:_MAX_ERROR_VALUE_CHARS]}... [truncated]"
