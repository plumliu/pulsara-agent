"""OpenAI-compatible retry error classification."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

from pulsara_agent.llm.retry import LLMRetryDecision, RetryDecisionKind


_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 413}
_RETRYABLE_EXCEPTION_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "TimeoutException",
    "TimeoutError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "NetworkError",
    "ConnectError",
    "ReadError",
    "WriteError",
    "RemoteProtocolError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "SSLError",
    "SSLWantReadError",
    "SSLWantWriteError",
    "SSLZeroReturnError",
    "ConnectionTerminated",
}
_TRANSPORT_MESSAGE_MARKERS = (
    "connection error",
    "connection reset",
    "connection aborted",
    "connection refused",
    "broken pipe",
    "read timeout",
    "write timeout",
    "connect timeout",
    "timed out",
    "timeout",
    "remote protocol",
    "stream closed",
    "server disconnected",
    "network is unreachable",
    "temporarily unavailable",
    "tls",
    "ssl",
    "socket",
)
_DETERMINISTIC_BAD_REQUEST_MARKERS = (
    "unknown parameter",
    "unsupported parameter",
    "invalid request",
    "invalid_request_error",
    "schema validation",
    "tool schema",
    "model not found",
    "does not support",
    "unsupported value",
    "context length",
    "context too long",
    "maximum context",
    "payload too large",
    "authentication",
    "insufficient_quota",
    "billing",
)
_NON_RETRYABLE_CODES = {
    "invalid_request_error",
    "unsupported_parameter",
    "unknown_parameter",
    "invalid_schema",
    "authentication",
    "insufficient_quota",
    "model_not_found",
    "context_length_exceeded",
}
_RETRY_AFTER_PATTERN = re.compile(
    r"(?:retry|try again|rate limit|too many requests).{0,80}?"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|millisecond|milliseconds|s|sec|secs|second|seconds)?",
    re.IGNORECASE,
)


def classify_llm_error(exc: BaseException) -> LLMRetryDecision:
    """Classify an OpenAI-compatible exception for transport retry."""

    status_code = _extract_status_code(exc)
    provider_code = _extract_provider_code(exc)
    retry_after_seconds = parse_retry_after_seconds(
        headers=_extract_headers(exc),
        body=_extract_body(exc),
        message=_chain_text(exc),
    )
    text = _chain_text(exc).lower()

    if _is_deterministic_bad_request(text, provider_code=provider_code):
        return LLMRetryDecision(
            kind=RetryDecisionKind.DO_NOT_RETRY,
            reason="deterministic_bad_request",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    if status_code in _NON_RETRYABLE_STATUS_CODES:
        return LLMRetryDecision(
            kind=RetryDecisionKind.DO_NOT_RETRY,
            reason=f"http_{status_code}_non_retryable",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    if status_code in _RETRYABLE_STATUS_CODES:
        return LLMRetryDecision(
            kind=RetryDecisionKind.RETRY,
            reason=f"http_{status_code}_retryable",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    if _has_retryable_transport_signal(exc):
        return LLMRetryDecision(
            kind=RetryDecisionKind.RETRY,
            reason="transport_error",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    return LLMRetryDecision(
        kind=RetryDecisionKind.DO_NOT_RETRY,
        reason="unknown_non_retryable",
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
        provider_code=provider_code,
    )


def parse_retry_after_seconds(
    *,
    headers: Mapping[str, Any] | None = None,
    body: Any = None,
    message: str | None = None,
) -> float | None:
    """Parse Retry-After hints from headers, body, or provider text."""

    header_value = _header_value(headers, "retry-after-ms")
    parsed = _parse_float_seconds(header_value, multiplier=0.001)
    if parsed is not None:
        return parsed

    header_value = _header_value(headers, "retry-after")
    parsed = _parse_retry_after_header(header_value)
    if parsed is not None:
        return parsed

    parsed = _parse_retry_after_from_body(body)
    if parsed is not None:
        return parsed

    if message:
        match = _RETRY_AFTER_PATTERN.search(message)
        if match:
            value = float(match.group("value"))
            unit = (match.group("unit") or "s").lower()
            if unit.startswith("ms") or unit.startswith("millisecond"):
                return value / 1000.0
            return value
    return None


def _extract_status_code(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        status = getattr(item, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(item, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status
    return None


def _extract_headers(exc: BaseException) -> Mapping[str, Any] | None:
    for item in _exception_chain(exc):
        response = getattr(item, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            return headers
        headers = getattr(item, "headers", None)
        if headers is not None:
            return headers
    return None


def _extract_body(exc: BaseException) -> Any:
    for item in _exception_chain(exc):
        body = getattr(item, "body", None)
        if body is not None:
            return body
        response = getattr(item, "response", None)
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                return json_method()
            except Exception:
                pass
    return None


def _extract_provider_code(exc: BaseException) -> str | None:
    for item in _exception_chain(exc):
        code = getattr(item, "code", None)
        if isinstance(code, str) and code:
            return code
        body_code = _provider_code_from_body(getattr(item, "body", None))
        if body_code:
            return body_code
    return _provider_code_from_body(_extract_body(exc))


def _provider_code_from_body(body: Any) -> str | None:
    if isinstance(body, Mapping):
        code = body.get("code")
        if isinstance(code, str) and code:
            return code
        error = body.get("error")
        if isinstance(error, Mapping):
            for key in ("code", "type"):
                code = error.get(key)
                if isinstance(code, str) and code:
                    return code
    return None


def _exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    for item in _exception_chain(exc):
        parts.append(type(item).__name__)
        parts.append(type(item).__module__)
        parts.append(str(item))
        body = getattr(item, "body", None)
        if body is not None:
            parts.append(str(body))
    extracted_body = _extract_body(exc)
    if extracted_body is not None:
        parts.append(str(extracted_body))
    return " ".join(part for part in parts if part)


def _is_deterministic_bad_request(text: str, *, provider_code: str | None) -> bool:
    if provider_code and provider_code.lower() in _NON_RETRYABLE_CODES:
        return True
    return any(marker in text for marker in _DETERMINISTIC_BAD_REQUEST_MARKERS)


def _has_retryable_transport_signal(exc: BaseException) -> bool:
    for item in _exception_chain(exc):
        if type(item).__name__ in _RETRYABLE_EXCEPTION_NAMES:
            return True
    text = _chain_text(exc).lower()
    return any(marker in text for marker in _TRANSPORT_MESSAGE_MARKERS)


def _header_value(headers: Mapping[str, Any] | None, name: str) -> Any:
    if not headers:
        return None
    lower_name = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lower_name:
            return value
    get = getattr(headers, "get", None)
    if callable(get):
        return get(name) or get(name.lower()) or get(name.title())
    return None


def _parse_float_seconds(value: Any, *, multiplier: float = 1.0) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return max(float(value) * multiplier, 0.0)
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(float(text) * multiplier, 0.0)
    except ValueError:
        return None


def _parse_retry_after_header(value: Any) -> float | None:
    parsed = _parse_float_seconds(value)
    if parsed is not None:
        return parsed
    if value is None:
        return None
    try:
        from datetime import datetime, timezone

        retry_at = parsedate_to_datetime(str(value))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except Exception:
        return None


def _parse_retry_after_from_body(body: Any) -> float | None:
    if isinstance(body, Mapping):
        for key in ("retry_after_ms", "retryAfterMs"):
            parsed = _parse_float_seconds(body.get(key), multiplier=0.001)
            if parsed is not None:
                return parsed
        for key in ("retry_after", "retryAfter"):
            parsed = _parse_float_seconds(body.get(key))
            if parsed is not None:
                return parsed
        for value in body.values():
            parsed = _parse_retry_after_from_body(value)
            if parsed is not None:
                return parsed
    elif isinstance(body, list | tuple):
        for value in body:
            parsed = _parse_retry_after_from_body(value)
            if parsed is not None:
                return parsed
    elif isinstance(body, str):
        return parse_retry_after_seconds(message=body)
    return None
